#!/usr/bin/env python3
"""
Game Board builder.

Builds site/index.html with two views:
  - "Today":                 every league's games for today only
  - "Today / This week":     today's MLB, NCAAB, NBA, NHL games + this week's NFL (Tue-Mon) and NCAAF (Mon-Sun)
  - "Yesterday / Last week": yesterday's daily games + last week's NFL and NCAAF
Data:
  - start times, teams, pitchers, neutral sites and live status from the MLB and NHL
    official schedule feeds (NFL from ESPN's scoreboard feed)
  - ROT numbers (and MLB pitcher handedness) from scoresandodds.com

Run by GitHub Actions on a schedule. Every run prints a short summary to the log,
so if something stops matching you can see which league/day it was.
"""
import html
import unicodedata
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

import props

TZ = ZoneInfo("America/Chicago")

# (label, scoresandodds path, "daily" or "weekly")
LEAGUE_INFO = [
    ("MLB", "mlb", "daily"),
    ("NCAAB", "ncaab", "daily"),
    ("NBA", "nba", "daily"),
    ("NCAAF", "ncaaf", "weekly"),
    ("NFL", "nfl", "weekly"),
    ("NHL", "nhl", "daily"),
]
LEAGUES = [(label, path) for label, path, _ in LEAGUE_INFO]
MODE = {label: mode for label, _, mode in LEAGUE_INFO}

# Season anchors (update each season): first day of NFL Week 1 (a Tuesday) and of
# NCAAF Week 0 (a Monday).
NFL_WEEK1_START = date(2026, 9, 8)
NCAAF_WEEK0_START = date(2026, 8, 24)

# Display-name overrides
NAME_OVERRIDES = {
    "Athletics": "Sacramento Athletics",
    "Oakland Athletics": "Sacramento Athletics",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def get(url, plain=False, extra_headers=None, **kw):
    """plain=True sends a normal script request; otherwise browser-like headers.
    If one style is refused, the other is tried once."""
    styles = [{}, HEADERS] if plain else [HEADERS, {}]
    last = None
    for h in styles:
        h = {**h, **(extra_headers or {})}
        try:
            r = requests.get(url, headers=h, timeout=25, **kw)
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            last = e
            if e.response is None or e.response.status_code not in (401, 403, 406, 429):
                raise
    raise last


# ---------------------------------------------------------------- schedules (times, teams)
# MLB and NHL use the leagues' own free feeds. NFL has no free official feed, so it
# tries ESPN on a few different hosts (ESPN blocks some cloud servers).

def to_ct(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def mlb_games(day):
    data = get("https://statsapi.mlb.com/api/v1/schedule", plain=True, params={
        "sportId": 1, "date": day.isoformat(), "hydrate": "probablePitcher,team,linescore",
    }).json()
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            start = to_ct(g["gameDate"])
            if start.date() != day:
                continue
            side = {}
            for k in ("away", "home"):
                t = g["teams"][k]
                team = t.get("team", {})
                full = team.get("name", "?")
                side[k] = {
                    "full": NAME_OVERRIDES.get(full, full),
                    "nick": team.get("teamName") or team.get("clubName") or "",
                    "pitcher": (t.get("probablePitcher") or {}).get("fullName"),
                    "abbr": team.get("abbreviation", ""),
                    "loc": team.get("locationName", ""),
                    "score": t.get("score"),
                }
            st = g.get("status", {})
            abstract = st.get("abstractGameState", "Preview")
            state = {"Live": "in", "Final": "post"}.get(abstract, "pre")
            detail = st.get("detailedState", "")
            ls = g.get("linescore") or {}
            if state == "in" and ls.get("currentInningOrdinal"):
                detail = f"{ls.get('inningHalf', '')} {ls['currentInningOrdinal']}".strip()
            note = None
            if g.get("gameType") not in (None, "R") and g.get("seriesDescription"):
                note = g["seriesDescription"]
                if g.get("seriesGameNumber"):
                    note += f", Game {g['seriesGameNumber']}"
            games.append({"start": start, "away": side["away"], "home": side["home"],
                          "state": state, "detail": detail, "neutral": False,
                          "city": None, "week": None, "extra": note})
    return games


def nhl_games(day):
    data = get(f"https://api-web.nhle.com/v1/schedule/{day.isoformat()}", plain=True).json()
    games = []
    for d in data.get("gameWeek", []):
        for g in d.get("games", []):
            start = to_ct(g["startTimeUTC"])
            if start.date() != day:
                continue
            side = {}
            for k, key in (("away", "awayTeam"), ("home", "homeTeam")):
                t = g.get(key, {})
                place = (t.get("placeName") or {}).get("default", "")
                nick = (t.get("commonName") or {}).get("default", "")
                full = (t.get("name") or {}).get("default") or f"{place} {nick}".strip()
                full = unicodedata.normalize("NFKD", full).encode("ascii", "ignore").decode()
                side[k] = {"full": full, "nick": nick, "pitcher": None, "abbr": t.get("abbrev", ""),
                           "loc": place, "score": t.get("score")}
            gs = g.get("gameState", "FUT")
            state = "post" if gs in ("FINAL", "OFF") else "in" if gs in ("LIVE", "CRIT") else "pre"
            detail = "Final" if state == "post" else "Live" if state == "in" else ""
            games.append({"start": start, "away": side["away"], "home": side["home"],
                          "state": state, "detail": detail,
                          "neutral": bool(g.get("neutralSite")),
                          "city": (g.get("venue") or {}).get("default"),
                          "week": None,
                          "extra": "Preseason" if g.get("gameType") == 1 else None})
    return games


def nfl_week_range(d):
    """NFL weeks run Tuesday-Monday."""
    start = d - timedelta(days=(d.weekday() - 1) % 7)
    return start, start + timedelta(days=6)


def ncaaf_week_range(d):
    """College football weeks run Monday-Sunday."""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def week_number(label, start):
    if label == "NFL":
        n = (start - NFL_WEEK1_START).days // 7 + 1
        return n if 1 <= n <= 22 else None
    n = (start - NCAAF_WEEK0_START).days // 7
    return n if 0 <= n <= 20 else None


ESPN_PATHS = {"NFL": ("football", "nfl", {}),
              "NCAAF": ("football", "college-football", {"groups": "80", "limit": "500"}),
              "NBA": ("basketball", "nba", {}),
              "NCAAB": ("basketball", "mens-college-basketball", {"groups": "50", "limit": "500"})}


ESPN_WORKING = {}   # remembers which ESPN address worked, per league, for this run


def espn_day(label, day):
    """One day of ESPN scoreboard data. ESPN blocks some cloud servers, so a few
    different addresses are tried (the one that works is remembered)."""
    sport, league, extra = ESPN_PATHS[label]
    params = {"dates": day.strftime("%Y%m%d"), **extra}
    attempts = [
        (f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard", True),
        (f"https://site.web.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard", True),
        (f"https://cdn.espn.com/core/{league}/scoreboard", True),
        (f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard", False),
    ]
    if label in ESPN_WORKING:
        attempts.insert(0, attempts[ESPN_WORKING[label]])
    last = None
    for i, (url, plain) in enumerate(attempts):
        try:
            p = dict(params, xhr="1") if "cdn.espn.com" in url else params
            data = get(url, plain=plain, params=p).json()
            if "content" in data:
                data = data["content"].get("sbData", {})
            if label not in ESPN_WORKING:
                ESPN_WORKING[label] = i
            return data.get("events", [])
        except Exception as e:
            last = e
    raise last


def espn_games(label, start_day, end_day):
    """Games from ESPN between two dates (inclusive), requested one day at a time."""
    wk = week_number(label, start_day) if MODE[label] == "weekly" else None
    games, seen, failures = [], set(), 0
    d = start_day
    while d <= end_day:
        try:
            events = espn_day(label, d)
        except Exception as e:
            failures += 1
            print(f"  [{label} {d}] ESPN failed: {e!r}")
            events = []
        for ev in events:
            try:
                if ev.get("id") in seen:
                    continue
                comp = ev["competitions"][0]
                start = to_ct(ev["date"])
                if not (start_day <= start.date() <= end_day):
                    continue
                side = {}
                for c in comp.get("competitors", []):
                    t = c.get("team", {})
                    side[c.get("homeAway")] = {"full": t.get("displayName", "?"),
                                               "nick": t.get("name") or "", "pitcher": None,
                                               "abbr": t.get("abbreviation", ""),
                                               "loc": t.get("location", ""),
                                               "score": c.get("score")}
                if "home" not in side or "away" not in side:
                    continue
                seen.add(ev.get("id"))
                st = ev.get("status", {}).get("type", {})
                venue = comp.get("venue", {}) or {}
                games.append({"start": start, "away": side["away"], "home": side["home"],
                              "state": st.get("state", "pre"), "detail": st.get("shortDetail", ""),
                              "neutral": bool(comp.get("neutralSite")),
                              "city": (venue.get("address") or {}).get("city"),
                              "week": wk, "extra": None})
            except Exception as e:
                print(f"  [{label} {d}] skipped one game: {e!r}")
        d += timedelta(days=1)
    if failures and not games:
        raise RuntimeError(f"ESPN unavailable for {failures} day(s)")
    return games


def load_games(label, start_day, end_day):
    if label == "MLB":
        return mlb_games(start_day)
    if label == "NHL":
        return nhl_games(start_day)
    return espn_games(label, start_day, end_day)





# ---------------------------------------------------------------- scoresandodds (ROT numbers)

PITCHER_RE = re.compile(r"([A-Z][^()\d]*?\s*\([LR]\))")


ODDS_KEYS = ("open", "ml", "tot", "line")


def header_map(tr):
    """Map column index -> 'ml' / 'tot' / 'line' using the table's header row."""
    table = tr.find_parent("table")
    if table is None:
        return {}
    thead = table.find("thead")
    hrow = thead.find("tr") if thead else table.find("tr")
    if hrow is None or hrow is tr:
        return {}
    m = {}
    for i, c in enumerate(hrow.find_all(["th", "td"], recursive=False)):
        t = norm(c.get_text(" ", strip=True))
        if t == "open":
            m[i] = "open"
        elif "moneyline" in t:
            m[i] = "ml"
        elif t.startswith("total"):
            m[i] = "tot"
        elif any(k in t for k in ("spread", "runline", "run line", "puckline", "puck line")):
            m[i] = "line"
    return m


def clean_odds(text):
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"\s*\+$", "", t).strip()      # the site's trailing "+" bet button
    return t or None


def matchup_url(tr, sao_path):
    """Find the game's Matchup link near its odds table (climbs a few levels, stops if it
    reaches a container holding more than one game)."""
    pat = re.compile(rf"^(?:https://www\.scoresandodds\.com)?/{sao_path}/[a-z0-9-]+-vs-[a-z0-9-]+/?$")
    node = tr.find_parent("table")
    for _ in range(5):
        if node is None:
            return None
        links = {a["href"] for a in node.find_all("a", href=pat)}
        if len(links) == 1:
            href = links.pop()
            return href if href.startswith("http") else "https://www.scoresandodds.com" + href
        if len(links) > 1:
            return None
        node = node.parent
    return None


def sao_pairs(sao_path, day):
    """Return [(away_row, home_row), ...] in page order. Each row: {rot, team, pitcher}."""
    r = get(f"https://www.scoresandodds.com/{sao_path}", params={"date": day.isoformat()})
    soup = BeautifulSoup(r.text, "html.parser")
    team_re = re.compile(rf"/{sao_path}/teams/([a-z0-9-]+)")
    rows, seen = [], set()
    for a in soup.find_all("a", href=team_re):
        tr = a.find_parent("tr")
        if tr is None:
            continue
        cell = tr.find(["td", "th"])
        if cell is None:
            continue
        cell_text = cell.get_text(" ", strip=True)
        m = re.match(r"(\d{1,4})\b", cell_text)
        if not m:
            continue
        rot = int(m.group(1))
        if rot in seen:
            continue
        seen.add(rot)
        rest = cell_text.replace(a.get_text(" ", strip=True), " ", 1)[m.end():]
        pm = PITCHER_RE.search(rest)
        odds = {}
        cells = tr.find_all(["td", "th"], recursive=False)
        for i, key in header_map(tr).items():
            if i < len(cells):
                v = clean_odds(cells[i].get_text(" ", strip=True))
                if v:
                    odds[key] = v
        rows.append({
            "rot": rot,
            "team": team_re.search(a["href"]).group(1),
            "pitcher": re.sub(r"\s+", " ", pm.group(1)).strip() if pm else None,
            "odds": odds,
            "url": matchup_url(tr, sao_path),
        })
    return [(rows[i], rows[i + 1]) for i in range(0, len(rows) - 1, 2)]


def team_matches(slug, team, loose=False):
    s = norm(slug)
    if not s:
        return False
    names = {norm(team.get("nick")), norm(team.get("loc")), norm(team.get("full"))}
    if s in names:
        return True
    return loose and (s in norm(team.get("full")) or norm(team.get("full")).startswith(s))


def attach_rots(games, pairs):
    """Match scoresandodds rows to games: exact team names first, then a looser pass."""
    used, matched = set(), 0
    for loose in (False, True):
        for g in games:
            if g.get("away_rot") is not None:
                continue
            for i, (a, h) in enumerate(pairs):
                if i in used:
                    continue
                if team_matches(a["team"], g["away"], loose) and team_matches(h["team"], g["home"], loose):
                    used.add(i)
                    g["away_rot"], g["home_rot"] = a["rot"], h["rot"]
                    g["away_odds"], g["home_odds"] = a.get("odds") or {}, h.get("odds") or {}
                    g["url"] = a.get("url") or h.get("url")
                    if a["pitcher"]:
                        g["away"]["pitcher"] = a["pitcher"]
                    if h["pitcher"]:
                        g["home"]["pitcher"] = h["pitcher"]
                    matched += 1
                    break
    return matched


# ---------------------------------------------------------------- props

BOOK_NAMES = {"riverscasino": "BetRivers", "betrivers": "BetRivers", "fanduel": "FanDuel",
              "draftkings": "DraftKings", "betmgm": "BetMGM", "caesars": "Caesars",
              "fanatics": "Fanatics", "bet365": "bet365", "hardrock": "Hard Rock",
              "espnbet": "ESPN BET", "borgata": "Borgata"}


def load_props():
    """Returns {league_label: [rows]} for today's props, direct first, browser if needed."""
    out, need_browser = {}, []
    for label, sao_path in LEAGUES:
        try:
            rows, ncats = props.fetch_direct(get, sao_path)
            print(f"[props {label}] direct: {ncats} categories, {len(rows)} props")
        except Exception as e:
            rows = []
            print(f"[props {label}] direct failed: {e!r}")
        if rows:
            out[label] = rows
        else:
            need_browser.append((label, sao_path))
    if need_browser:
        try:
            got = props.fetch_browser([sp for _, sp in need_browser])
            for label, sp in need_browser:
                out[label] = got.get(sp, [])
                print(f"[props {label}] browser: {len(out[label])} props")
        except Exception as e:
            print(f"[props] browser fallback failed: {e!r}")
    return out


def attach_props(label, games, rows):
    index = {}
    for g in games:
        key = frozenset({props.canon(label, g["away"].get("abbr")),
                         props.canon(label, g["home"].get("abbr"))})
        index[key] = g
    unmatched, matched = set(), []
    for r in rows:
        key = frozenset({props.canon(label, r["team"]), props.canon(label, r["opp"])})
        g = index.get(key)
        if g is None:
            unmatched.add(f"{r['team']}-{r['opp']}")
            continue
        matched.append((g, r))
    try:
        filled, reqs = props.add_comparisons(get, [r for _, r in matched])
        print(f"[props {label}] sportsbook comparison: {filled} of {len(matched)} props "
              f"({reqs} game/category groups)")
    except Exception as e:
        print(f"[props {label}] sportsbook comparison failed: {e!r}")
    for g, r in matched:
        entry = {
            "p": r["player"], "t": r["team"],
            "o": [[x["line"], x["odds"], BOOK_NAMES.get(x["book"], x["book"])] for x in r["prices"]],
        }
        if r.get("books"):
            try:
                bo, bu = props.best_books(r["books"])
            except Exception:
                bo = bu = None
            entry["b"] = r["books"]
            entry["bo"], entry["bu"] = bo, bu
        g.setdefault("props", {}).setdefault(r["cat"], []).append(entry)
    n = sum(len(v) for g in games for v in (g.get("props") or {}).values())
    msg = f"[props {label}] attached {n} of {len(rows)}"
    if unmatched:
        msg += f"; not on today's board: {', '.join(sorted(unmatched)[:8])}"
    print(msg)


# ---------------------------------------------------------------- build

SAO_CACHE = {}


def sao_pairs_range(sao_path, start_day, end_day):
    """ROT rows for every day in a range, de-duplicated (weekly pages may repeat games)."""
    out, seen = [], set()
    d = start_day
    while d <= end_day:
        key = (sao_path, d)
        if key not in SAO_CACHE:
            try:
                SAO_CACHE[key] = sao_pairs(sao_path, d)
            except Exception as e:
                print(f"  [{sao_path} {d}] scoresandodds failed: {e!r}")
                SAO_CACHE[key] = []
        for pair in SAO_CACHE[key]:
            if pair[0]["rot"] not in seen:
                seen.add(pair[0]["rot"])
                out.append(pair)
        d += timedelta(days=1)
    return out


def game_row(label, sao_path, g, weekly):
    return {
        "lg": label,
        "t": g["start"].isoformat(),
        "time": g["start"].strftime("%-I:%M %p"),
        "dl": g["start"].strftime("%a %-m/%-d") if weekly else None,
        "ar": g.get("away_rot"), "hr": g.get("home_rot"),
        "ao": g.get("away_odds") or {}, "ho": g.get("home_odds") or {},
        "as": g["away"].get("score"), "hs": g["home"].get("score"),
        "url": g.get("url"),
        "day_url": f"https://www.scoresandodds.com/{sao_path}?date={g['start'].date().isoformat()}",
        "a": g["away"]["full"], "ap": g["away"]["pitcher"] if label == "MLB" else None,
        "h": g["home"]["full"], "hp": g["home"]["pitcher"] if label == "MLB" else None,
        "state": g["state"], "detail": g["detail"],
        "props": g.get("props") or {},
        "note": " · ".join(filter(None, [
            g.get("extra"),
            f"Week {g['week']}" if weekly and g.get("week") is not None else None,
            f"Neutral site: {g['city']}" if g["neutral"] and g["city"] else None,
        ])),
    }


def build_view(key, title, anchor_day, weekly_anchor, props_by_league, notes):
    rows, ranges = [], []
    for label, sao_path in LEAGUES:
        weekly = MODE[label] == "weekly"
        if weekly:
            start_day, end_day = (nfl_week_range if label == "NFL" else ncaaf_week_range)(weekly_anchor)
        else:
            start_day = end_day = anchor_day
        tag = f"{key} {label} {start_day:%-m/%-d}" + (f"-{end_day:%-m/%-d}" if weekly else "")
        try:
            games = load_games(label, start_day, end_day)
        except Exception as e:
            print(f"[{tag}] schedule failed: {e}")
            notes.append(f"{label} schedule couldn't be loaded this run.")
            continue
        if weekly:
            wk = week_number(label, start_day)
            if games and wk is not None:
                ranges.append(f"{label} Week {wk} ({start_day:%-m/%-d}–{end_day:%-m/%-d})")
        if not games:
            print(f"[{tag}] no games")
            continue
        try:
            pairs = sao_pairs_range(sao_path, start_day, end_day)
            matched = attach_rots(games, pairs)
            with_odds = sum(1 for g in games if g.get("away_odds") or g.get("home_odds"))
            print(f"[{tag}] {len(games)} games, {len(pairs)} ROT pairs, "
                  f"{matched} matched, {with_odds} with odds")
        except Exception as e:
            print(f"[{tag}] scoresandodds failed: {e}")
        if props_by_league.get(label):
            attach_props(label, games, props_by_league[label])
        rows.extend(game_row(label, sao_path, g, weekly) for g in games)
    rows.sort(key=lambda x: (x["t"], x["ar"] or 99999))
    return {"key": key, "label": title,
            "long": " · ".join([anchor_day.strftime("%A, %B %-d")] + ranges),
            "games": rows}


def build():
    now = datetime.now(TZ)
    today = now.date()
    notes = []
    try:
        todays_props = load_props()
    except Exception as e:
        print(f"[props] failed: {e!r}")
        todays_props = {}

    current = build_view("current", "Today / This week", today, today, todays_props, notes)
    previous = build_view("previous", "Yesterday / Last week", today - timedelta(days=1),
                          today - timedelta(days=7), {}, notes)
    # "Today": every league, today's games only (taken from the current view)
    today_only = {
        "key": "today", "label": "Today",
        "long": today.strftime("%A, %B %-d"),
        "games": [dict(g, dl=None) for g in current["games"]
                  if g["t"][:10] == today.isoformat()],
    }
    views = [previous, today_only, current]
    payload = {
        "start_tab": 1,   # open on "Today"
        "updated": now.strftime("%-I:%M %p CDT, %a %-m/%-d"),
        "days": views,
        "notes": sorted(set(notes)),
    }
    template = Path(__file__).with_name("board_template.html").read_text(encoding="utf-8")
    page = template.replace("__DATA__", json.dumps(payload).replace("</", "<\\/"))
    Path("site").mkdir(exist_ok=True)
    Path("site/index.html").write_text(page, encoding="utf-8")
    total = len(current["games"]) + len(previous["games"])
    print(f"Wrote site/index.html with {total} games at {payload['updated']}")


if __name__ == "__main__":
    try:
        build()
    except Exception as e:
        print(f"Build failed: {e}")
        sys.exit(1)
