#!/usr/bin/env python3
"""
Game Board builder.

Builds site/index.html with today's and tomorrow's MLB, NFL and NHL games:
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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("America/Chicago")
DAYS_AHEAD = 1  # 0 = today only, 1 = today + tomorrow

# (label, scoresandodds path)
LEAGUES = [("MLB", "mlb"), ("NFL", "nfl"), ("NHL", "nhl")]

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


def get(url, plain=False, **kw):
    """plain=True sends a normal script request; otherwise browser-like headers.
    If one style is refused, the other is tried once."""
    styles = [{}, HEADERS] if plain else [HEADERS, {}]
    last = None
    for h in styles:
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
                side[k] = {"full": full, "nick": nick, "pitcher": None}
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


def nfl_week(day):
    # 2026 season: Week 1 runs Tue 9/8 - Mon 9/14
    from datetime import date
    n = (day - date(2026, 9, 8)).days // 7 + 1
    return n if 1 <= n <= 18 else None


def nfl_games(day):
    ds = day.strftime("%Y%m%d")
    attempts = [
        (f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={ds}", True),
        (f"https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={ds}", True),
        (f"https://cdn.espn.com/core/nfl/scoreboard?xhr=1&dates={ds}", True),
        (f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={ds}", False),
    ]
    last = None
    for url, plain in attempts:
        try:
            data = get(url, plain=plain).json()
            if "content" in data:
                data = data["content"].get("sbData", {})
            break
        except Exception as e:
            last = e
    else:
        raise last
    games = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        start = to_ct(ev["date"])
        if start.date() != day:
            continue
        side = {}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            side[c.get("homeAway")] = {"full": t.get("displayName", "?"),
                                       "nick": t.get("name") or "", "pitcher": None}
        if "home" not in side or "away" not in side:
            continue
        st = ev.get("status", {}).get("type", {})
        venue = comp.get("venue", {}) or {}
        games.append({"start": start, "away": side["away"], "home": side["home"],
                      "state": st.get("state", "pre"), "detail": st.get("shortDetail", ""),
                      "neutral": bool(comp.get("neutralSite")),
                      "city": (venue.get("address") or {}).get("city"),
                      "week": nfl_week(day), "extra": None})
    return games


SCHEDULES = {"MLB": mlb_games, "NFL": nfl_games, "NHL": nhl_games}


# ---------------------------------------------------------------- scoresandodds (ROT numbers)

PITCHER_RE = re.compile(r"([A-Z][^()\d]*?\s*\([LR]\))")


ODDS_KEYS = ("ml", "tot", "line")


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
        if "moneyline" in t:
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
        })
    return [(rows[i], rows[i + 1]) for i in range(0, len(rows) - 1, 2)]


def team_matches(slug, team):
    s = norm(slug)
    return bool(s) and (s == norm(team["nick"]) or s in norm(team["full"]))


def attach_rots(games, pairs):
    used = set()
    matched = 0
    for g in games:
        for i, (a, h) in enumerate(pairs):
            if i in used:
                continue
            if team_matches(a["team"], g["away"]) and team_matches(h["team"], g["home"]):
                used.add(i)
                g["away_rot"], g["home_rot"] = a["rot"], h["rot"]
                g["away_odds"], g["home_odds"] = a.get("odds") or {}, h.get("odds") or {}
                if a["pitcher"]:
                    g["away"]["pitcher"] = a["pitcher"]
                if h["pitcher"]:
                    g["home"]["pitcher"] = h["pitcher"]
                matched += 1
                break
    return matched


# ---------------------------------------------------------------- build

def build():
    now = datetime.now(TZ)
    days = [now.date() + timedelta(days=i) for i in range(DAYS_AHEAD + 1)]
    out_days, notes = [], []

    for day in days:
        day_games = []
        for label, sao_path in LEAGUES:
            try:
                games = SCHEDULES[label](day)
            except Exception as e:
                print(f"[{day} {label}] schedule failed: {e}")
                notes.append(f"{label} schedule for {day:%-m/%-d} couldn't be loaded this run.")
                continue
            if not games:
                print(f"[{day} {label}] no games")
                continue
            try:
                pairs = sao_pairs(sao_path, day)
                matched = attach_rots(games, pairs)
                with_odds = sum(1 for g in games if g.get("away_odds") or g.get("home_odds"))
                print(f"[{day} {label}] {len(games)} games, {len(pairs)} ROT pairs, "
                      f"{matched} matched, {with_odds} with odds")
                if matched < len(games):
                    notes.append(f"Some {label} ROT numbers for {day:%-m/%-d} aren't posted yet.")
            except Exception as e:
                print(f"[{day} {label}] scoresandodds failed: {e}")
                notes.append(f"{label} ROT numbers for {day:%-m/%-d} couldn't be loaded this run.")
            for g in games:
                day_games.append({
                    "lg": label,
                    "t": g["start"].isoformat(),
                    "time": g["start"].strftime("%-I:%M %p"),
                    "ar": g.get("away_rot"), "hr": g.get("home_rot"),
                    "ao": g.get("away_odds") or {}, "ho": g.get("home_odds") or {},
                    "a": g["away"]["full"], "ap": g["away"]["pitcher"] if label == "MLB" else None,
                    "h": g["home"]["full"], "hp": g["home"]["pitcher"] if label == "MLB" else None,
                    "state": g["state"], "detail": g["detail"],
                    "note": " · ".join(filter(None, [
                        g.get("extra"),
                        f"Week {g['week']}" if label == "NFL" and g["week"] else None,
                        f"Neutral site: {g['city']}" if g["neutral"] and g["city"] else None,
                    ])),
                })
        day_games.sort(key=lambda x: (x["t"], x["ar"] or 99999))
        out_days.append({
            "key": day.isoformat(),
            "label": "Today" if day == now.date() else day.strftime("%A"),
            "long": day.strftime("%A, %B %-d, %Y"),
            "games": day_games,
        })

    payload = {
        "updated": now.strftime("%-I:%M %p CDT, %a %-m/%-d"),
        "days": out_days,
        "notes": sorted(set(notes)),
    }
    template = Path(__file__).with_name("board_template.html").read_text(encoding="utf-8")
    page = template.replace("__DATA__", json.dumps(payload).replace("</", "<\\/"))
    Path("site").mkdir(exist_ok=True)
    Path("site/index.html").write_text(page, encoding="utf-8")
    total = sum(len(d["games"]) for d in out_days)
    print(f"Wrote site/index.html with {total} games at {payload['updated']}")


if __name__ == "__main__":
    try:
        build()
    except Exception as e:
        print(f"Build failed: {e}")
        sys.exit(1)
