#!/usr/bin/env python3
"""
Game Board builder.

Builds site/index.html with two views:
  - "Today":                 every league's games for today only
  - "Today / This Week":     today's MLB, NCAAB, NBA, NHL games + this week's NFL (Tue-Mon) and NCAAF (Mon-Sun)
  - "Yesterday / Last Week": yesterday's daily games + last week's NFL and NCAAF
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
            if state == "post":
                detail = "Final"
                try:
                    inn = int(ls.get("currentInning") or 0)
                    sched = int(ls.get("scheduledInnings") or 9)
                    if inn > sched:
                        detail = f"Final ({inn})"
                except (TypeError, ValueError):
                    pass
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
            games.append({"id": g.get("id"), "start": start, "away": side["away"], "home": side["home"],
                          "state": state, "detail": detail,
                          "neutral": bool(g.get("neutralSite")),
                          "city": (g.get("venue") or {}).get("default"),
                          "week": None,
                          "extra": "Preseason" if g.get("gameType") == 1 else None})
    if any(x["state"] != "pre" for x in games):
        try:
            nhl_add_clock(day, games)
        except Exception as e:
            print(f"  [NHL {day}] scoreboard clock failed: {e!r}")
    return games


def _ordinal(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def ot_label(text):
    """'OT' -> 'OT', '2OT' -> '2 OT'."""
    m = re.match(r"(\d+)\s*OT", text or "", re.I)
    return f"{m.group(1)} OT" if m and m.group(1) != "1" else "OT"


REG_PERIODS = {"NFL": 4, "NCAAF": 4, "NBA": 4, "NCAAB": 2}


def event_name(comp):
    """Special-event name ESPN attaches to a game, e.g. 'Hall of Fame Series' or a bowl name."""
    for n in comp.get("notes") or []:
        h = (n or {}).get("headline")
        if h:
            return h.strip()
    return None


def venue_text(venue):
    """'AT&T Stadium, Arlington, TX' from ESPN's venue info (whatever parts it has)."""
    venue = venue or {}
    addr = venue.get("address") or {}
    city, state = addr.get("city"), addr.get("state")
    place = ", ".join(p for p in (city, state) if p)
    name = venue.get("fullName")
    if name and place:
        return f"{name}, {place}"
    return name or place or None


def espn_final_label(label, ev):
    """'Final', 'Final (OT)', 'Final (2 OT)' from ESPN's period count (falls back to its text)."""
    status = ev.get("status") or {}
    try:
        extra = int(status.get("period") or 0) - REG_PERIODS.get(label, 4)
    except (TypeError, ValueError):
        extra = 0
    if extra <= 0:
        m = re.search(r"(\d*)\s*OT", (status.get("type") or {}).get("shortDetail", ""), re.I)
        if not m:
            return "Final"
        extra = int(m.group(1) or 1)
    return "Final (OT)" if extra == 1 else f"Final ({extra} OT)"


def nhl_period_label(pd):
    ptype = (pd or {}).get("periodType", "REG")
    num = (pd or {}).get("number") or 0
    if ptype == "SO":
        return "SO"
    if ptype == "OT":
        ot = num - 3
        return "OT" if ot <= 1 else f"{ot}OT"
    return _ordinal(num) if num else ""


def nhl_add_clock(day, games):
    """Scoreboard clock for NHL games (period, time left, intermission), like ESPN's
    '12:34 - 2nd'. The schedule feed only says the game is live."""
    data = get(f"https://api-web.nhle.com/v1/score/{day.isoformat()}", plain=True).json()
    by_id = {g.get("id"): g for g in data.get("games", [])}
    for x in games:
        g = by_id.get(x.get("id"))
        if not g:
            continue
        per = nhl_period_label(g.get("periodDescriptor"))
        clock = g.get("clock") or {}
        if x["state"] == "in":
            if clock.get("inIntermission"):
                x["detail"] = f"End {per}" if per else "Intermission"
            elif per == "SO":
                x["detail"] = "Shootout"
            elif clock.get("timeRemaining"):
                x["detail"] = f"{clock['timeRemaining']} - {per}" if per else clock["timeRemaining"]
        elif x["state"] == "post" and (per == "SO" or per.endswith("OT")):
            x["detail"] = "Final (SO)" if per == "SO" else f"Final ({ot_label(per)})"
        for k in ("away", "home"):
            sc = (g.get(f"{k}Team") or {}).get("score")
            if sc is not None:
                x[k]["score"] = sc


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


def college_rank(c):
    """AP/CFP-style ranking from ESPN (99 means unranked)."""
    try:
        r = int((c.get("curatedRank") or {}).get("current") or 99)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= 25 else None


# Pro conferences/divisions (update if a league realigns)
PRO_DIVISIONS = {
    "NFL": {
        "Buffalo Bills": "AFC East",
        "Miami Dolphins": "AFC East",
        "New England Patriots": "AFC East",
        "New York Jets": "AFC East",
        "Baltimore Ravens": "AFC North",
        "Cincinnati Bengals": "AFC North",
        "Cleveland Browns": "AFC North",
        "Pittsburgh Steelers": "AFC North",
        "Houston Texans": "AFC South",
        "Indianapolis Colts": "AFC South",
        "Jacksonville Jaguars": "AFC South",
        "Tennessee Titans": "AFC South",
        "Denver Broncos": "AFC West",
        "Kansas City Chiefs": "AFC West",
        "Las Vegas Raiders": "AFC West",
        "Los Angeles Chargers": "AFC West",
        "Dallas Cowboys": "NFC East",
        "New York Giants": "NFC East",
        "Philadelphia Eagles": "NFC East",
        "Washington Commanders": "NFC East",
        "Chicago Bears": "NFC North",
        "Detroit Lions": "NFC North",
        "Green Bay Packers": "NFC North",
        "Minnesota Vikings": "NFC North",
        "Atlanta Falcons": "NFC South",
        "Carolina Panthers": "NFC South",
        "New Orleans Saints": "NFC South",
        "Tampa Bay Buccaneers": "NFC South",
        "Arizona Cardinals": "NFC West",
        "Los Angeles Rams": "NFC West",
        "San Francisco 49ers": "NFC West",
        "Seattle Seahawks": "NFC West",
    },
    "NBA": {
        "Boston Celtics": "East · Atlantic",
        "Brooklyn Nets": "East · Atlantic",
        "New York Knicks": "East · Atlantic",
        "Philadelphia 76ers": "East · Atlantic",
        "Toronto Raptors": "East · Atlantic",
        "Chicago Bulls": "East · Central",
        "Cleveland Cavaliers": "East · Central",
        "Detroit Pistons": "East · Central",
        "Indiana Pacers": "East · Central",
        "Milwaukee Bucks": "East · Central",
        "Atlanta Hawks": "East · Southeast",
        "Charlotte Hornets": "East · Southeast",
        "Miami Heat": "East · Southeast",
        "Orlando Magic": "East · Southeast",
        "Washington Wizards": "East · Southeast",
        "Denver Nuggets": "West · Northwest",
        "Minnesota Timberwolves": "West · Northwest",
        "Oklahoma City Thunder": "West · Northwest",
        "Portland Trail Blazers": "West · Northwest",
        "Utah Jazz": "West · Northwest",
        "Golden State Warriors": "West · Pacific",
        "LA Clippers": "West · Pacific",
        "Los Angeles Clippers": "West · Pacific",
        "Los Angeles Lakers": "West · Pacific",
        "Phoenix Suns": "West · Pacific",
        "Sacramento Kings": "West · Pacific",
        "Dallas Mavericks": "West · Southwest",
        "Houston Rockets": "West · Southwest",
        "Memphis Grizzlies": "West · Southwest",
        "New Orleans Pelicans": "West · Southwest",
        "San Antonio Spurs": "West · Southwest",
    },
    "NHL": {
        "Boston Bruins": "East · Atlantic",
        "Buffalo Sabres": "East · Atlantic",
        "Detroit Red Wings": "East · Atlantic",
        "Florida Panthers": "East · Atlantic",
        "Montreal Canadiens": "East · Atlantic",
        "Ottawa Senators": "East · Atlantic",
        "Tampa Bay Lightning": "East · Atlantic",
        "Toronto Maple Leafs": "East · Atlantic",
        "Carolina Hurricanes": "East · Metropolitan",
        "Columbus Blue Jackets": "East · Metropolitan",
        "New Jersey Devils": "East · Metropolitan",
        "New York Islanders": "East · Metropolitan",
        "New York Rangers": "East · Metropolitan",
        "Philadelphia Flyers": "East · Metropolitan",
        "Pittsburgh Penguins": "East · Metropolitan",
        "Washington Capitals": "East · Metropolitan",
        "Chicago Blackhawks": "West · Central",
        "Colorado Avalanche": "West · Central",
        "Dallas Stars": "West · Central",
        "Minnesota Wild": "West · Central",
        "Nashville Predators": "West · Central",
        "St. Louis Blues": "West · Central",
        "Utah Mammoth": "West · Central",
        "Utah Hockey Club": "West · Central",
        "Winnipeg Jets": "West · Central",
        "Anaheim Ducks": "West · Pacific",
        "Calgary Flames": "West · Pacific",
        "Edmonton Oilers": "West · Pacific",
        "Los Angeles Kings": "West · Pacific",
        "San Jose Sharks": "West · Pacific",
        "Seattle Kraken": "West · Pacific",
        "Vancouver Canucks": "West · Pacific",
        "Vegas Golden Knights": "West · Pacific",
    },
    "MLB": {
        "Baltimore Orioles": "AL East",
        "Boston Red Sox": "AL East",
        "New York Yankees": "AL East",
        "Tampa Bay Rays": "AL East",
        "Toronto Blue Jays": "AL East",
        "Chicago White Sox": "AL Central",
        "Cleveland Guardians": "AL Central",
        "Detroit Tigers": "AL Central",
        "Kansas City Royals": "AL Central",
        "Minnesota Twins": "AL Central",
        "Houston Astros": "AL West",
        "Los Angeles Angels": "AL West",
        "Athletics": "AL West",
        "Sacramento Athletics": "AL West",
        "Oakland Athletics": "AL West",
        "Seattle Mariners": "AL West",
        "Texas Rangers": "AL West",
        "Atlanta Braves": "NL East",
        "Miami Marlins": "NL East",
        "New York Mets": "NL East",
        "Philadelphia Phillies": "NL East",
        "Washington Nationals": "NL East",
        "Chicago Cubs": "NL Central",
        "Cincinnati Reds": "NL Central",
        "Milwaukee Brewers": "NL Central",
        "Pittsburgh Pirates": "NL Central",
        "St. Louis Cardinals": "NL Central",
        "Arizona Diamondbacks": "NL West",
        "Colorado Rockies": "NL West",
        "Los Angeles Dodgers": "NL West",
        "San Diego Padres": "NL West",
        "San Francisco Giants": "NL West",
    },
}


# ---------------------------------------------------------------- records
# Overall and division records. College records (overall + conference) come with ESPN's
# scoreboard; pro division records come from ESPN's standings (one request per league).

STANDINGS = {}            # label -> {normalized team name: (overall, division)}
STANDINGS_LOADED = set()
STANDINGS_PATHS = {"NFL": ("football", "nfl"), "NBA": ("basketball", "nba"),
                   "NHL": ("hockey", "nhl"), "MLB": ("baseball", "mlb")}
WL_RE = re.compile(r"^\d{1,3}-\d{1,3}(?:-\d{1,3})?$")


def team_key(name):
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def scoreboard_record(c, kind):
    for r in c.get("records") or []:
        typ = (r.get("type") or "").lower()
        name = (r.get("name") or "").lower()
        if (kind == "total" and (typ == "total" or name == "overall")) or \
           (kind == "vsconf" and (typ == "vsconf" or "conf" in name)):
            v = r.get("summary") or r.get("displayValue")
            if v and WL_RE.match(v.strip()):
                return v.strip()
    return None


def _stat_wl(stat):
    v = (stat.get("summary") or stat.get("displayValue") or "").strip()
    return v if WL_RE.match(v) else None


def _entries(node):
    if isinstance(node, dict):
        st = node.get("standings")
        if isinstance(st, dict) and isinstance(st.get("entries"), list):
            yield from st["entries"]
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _entries(v)
    elif isinstance(node, list):
        for v in node:
            yield from _entries(v)


def load_standings(label):
    if label in STANDINGS_LOADED or label not in STANDINGS_PATHS:
        return
    STANDINGS_LOADED.add(label)
    sport, league = STANDINGS_PATHS[label]
    table = {}
    for host in ("site.api.espn.com", "site.web.api.espn.com"):
        try:
            data = get(f"https://{host}/apis/v2/sports/{sport}/{league}/standings", plain=True,
                       params={"level": "3"}).json()
        except Exception:
            continue
        for e in _entries(data):
            team = e.get("team") or {}
            overall = div = None
            nums = {}
            for stat in e.get("stats") or []:
                name = (stat.get("name") or "").lower()
                typ = (stat.get("type") or "").lower()
                wl = _stat_wl(stat)
                if wl and (typ == "total" or name == "overall"):
                    overall = wl
                elif wl and (typ in ("vsdiv", "division") or ("div" in name and "percent" not in name)):
                    div = wl
                if name in ("wins", "losses", "ties", "otlosses", "overtimelosses") and stat.get("value") is not None:
                    nums[name] = int(stat["value"])
            if not overall and "wins" in nums and "losses" in nums:
                extra = nums.get("otlosses", nums.get("overtimelosses", nums.get("ties")))
                overall = f"{nums['wins']}-{nums['losses']}" + (f"-{extra}" if extra else "")
            for nm in (team.get("displayName"), team.get("name"), team.get("shortDisplayName")):
                if nm:
                    table.setdefault(team_key(nm), (overall, div))
        if table:
            break
    STANDINGS[label] = table
    with_div = sum(1 for v in table.values() if v[1])
    print(f"[records {label}] standings for {len(table)} names, {with_div} with division records")


def record_text(label, team):
    """'6-2 (2-1 div)' for pros, '7-1 (4-1 conf)' for college."""
    if label in ("NCAAF", "NCAAB"):
        rec, sub, tag = team.get("rec") or team.get("sao_rec"), team.get("crec"), "conf"
    else:
        load_standings(label)
        st = STANDINGS.get(label, {})
        found = st.get(team_key(team.get("full"))) or st.get(team_key(team.get("nick"))) or (None, None)
        rec = found[0] or team.get("rec") or team.get("sao_rec")
        sub, tag = found[1], "div"
    if not rec:
        return None
    return f"{rec} ({sub} {tag})" if sub else rec


def team_info(label, team):
    """Conference/division plus record, e.g. 'NFC North · 6-2 (2-1 div)'."""
    conf = team.get("conf") or PRO_DIVISIONS.get(label, {}).get(team.get("full"))
    rec = record_text(label, team)
    return " · ".join(x for x in (conf, rec) if x) or None


def mascot(label, team):
    """College team's mascot ('Vikings' from 'Cleveland State Vikings'), shown on the board
    as decoration so the browser's find (Cmd+F) skips it."""
    if label not in ("NCAAF", "NCAAB"):
        return None
    full, loc = team.get("full") or "", team.get("loc") or ""
    if loc and full.startswith(loc + " ") and len(full) > len(loc) + 1:
        return full[len(loc) + 1:]
    return None


def copy_text(label, g, weekly):
    """Plain text for the copy button, e.g. 'Wisconsin Badgers (25) @ Iowa Hawkeyes - Week 9'."""
    def name(side):
        t = g[side]
        # College football and basketball: school only ("Oregon State"), no mascot
        base = (t.get("loc") or t["full"]) if label in ("NCAAF", "NCAAB") else t["full"]
        return f"{base} ({t['rank']})" if t.get("rank") else base
    parts = [f"{name('away')} @ {name('home')}"]
    if weekly and g.get("week") is not None:
        parts.append(f"Week {g['week']}")
    venue = place = None
    if g.get("neutral"):
        venue = g.get("venue_name") or (g.get("city") if label == "NHL" else None)
        place = g.get("place")                         # e.g. "Las Vegas, NV"
    if g.get("event"):
        # ESPN sometimes adds the city to the event ("Hall of Fame Series - Las Vegas");
        # drop any piece that repeats the arena or city shown after it.
        where = " ".join(x for x in (venue, place) if x).lower()
        pieces = [x.strip() for x in g["event"].split(" - ")]
        pieces = [x for x in pieces if x and (not where or x.lower() not in where)]
        if pieces:
            parts.append(" - ".join(pieces))           # e.g. "Hall of Fame Series"
    parts += [venue, place]
    return " - ".join(p for p in parts if p)


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
                                               "score": c.get("score"),
                                               "rank": college_rank(c),
                                               "conf_id": str(t.get("conferenceId") or "") or None,
                                               "rec": scoreboard_record(c, "total"),
                                               "crec": scoreboard_record(c, "vsconf")}
                if "home" not in side or "away" not in side:
                    continue
                seen.add(ev.get("id"))
                st = ev.get("status", {}).get("type", {})
                venue = comp.get("venue", {}) or {}
                games.append({"start": start, "away": side["away"], "home": side["home"],
                              "state": st.get("state", "pre"),
                              "detail": espn_final_label(label, ev) if st.get("state") == "post" else st.get("shortDetail", ""),
                              "neutral": bool(comp.get("neutralSite")),
                              "city": venue_text(venue),
                              "venue_name": venue.get("fullName"),
                              "place": ", ".join(x for x in ((venue.get("address") or {}).get("city"),
                                                             (venue.get("address") or {}).get("state")) if x) or None,
                              "event": event_name(comp),
                              "week": wk, "extra": event_name(comp)})
            except Exception as e:
                print(f"  [{label} {d}] skipped one game: {e!r}")
        d += timedelta(days=1)
    if failures and not games:
        raise RuntimeError(f"ESPN unavailable for {failures} day(s)")
    if label in ("NCAAF", "NCAAB") and games:
        try:
            attach_conferences(label, games)
        except Exception as e:
            print(f"  [{label}] conference names failed: {e!r}")
    return games


# ---------------------------------------------------------------- college conferences
# ESPN's scoreboard gives each school's conference as an ID; names come from ESPN's
# conference list (fetched once per run), or one lookup per unknown ID as a fallback.

CONF_NAMES = {}          # (label, id) -> short name, e.g. "Big Ten"
CONF_LIST_LOADED = set()
CONF_LOOKUP_LIMIT = 60


def _collect_groups(node, label):
    if isinstance(node, dict):
        gid = node.get("groupId") or node.get("id")
        name = node.get("shortName") or node.get("abbreviation") or node.get("name")
        if gid and name and ("conference" in str(node.get("uid", "")) or node.get("isConference")
                             or "shortName" in node):
            CONF_NAMES.setdefault((label, str(gid)), str(name))
        for v in node.values():
            _collect_groups(v, label)
    elif isinstance(node, list):
        for v in node:
            _collect_groups(v, label)


def load_conference_list(label):
    if label in CONF_LIST_LOADED:
        return
    CONF_LIST_LOADED.add(label)
    sport, league, _ = ESPN_PATHS[label]
    for url in (f"https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/groups",
                f"https://site.web.api.espn.com/apis/site/v2/sports/{sport}/{league}/groups"):
        try:
            _collect_groups(get(url, plain=True).json(), label)
            if any(k[0] == label for k in CONF_NAMES):
                return
        except Exception:
            continue


def conference_name(label, cid):
    if not cid:
        return None
    key = (label, cid)
    if key in CONF_NAMES:
        return CONF_NAMES[key]
    if sum(1 for k in CONF_NAMES if k[0] == label) < CONF_LOOKUP_LIMIT:
        sport, league, _ = ESPN_PATHS[label]
        try:
            d = get(f"https://sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/groups/{cid}",
                    plain=True, params={"lang": "en"}).json()
            name = d.get("shortName") or d.get("abbreviation") or d.get("name")
            CONF_NAMES[key] = name
            return name
        except Exception:
            pass
    CONF_NAMES[key] = None
    return None


def attach_conferences(label, games):
    load_conference_list(label)
    named = 0
    for g in games:
        for side in ("away", "home"):
            t = g[side]
            t["conf"] = conference_name(label, t.get("conf_id"))
            named += bool(t["conf"])
    print(f"  [{label}] conferences named for {named} of {2 * len(games)} teams")


def load_games(label, start_day, end_day):
    if label == "MLB":
        return mlb_games(start_day)
    if label == "NHL":
        return nhl_games(start_day)
    return espn_games(label, start_day, end_day)





# ---------------------------------------------------------------- scoresandodds (ROT numbers)

RECORD_RE = re.compile(r"(?<![\d.])(\d{1,3}-\d{1,3}(?:-\d{1,3})?)(?![\d.])")
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
            "rec": (lambda m2: m2.group(1) if m2 else None)(RECORD_RE.search(PITCHER_RE.sub(" ", rest))),
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
                    g["away"].setdefault("sao_rec", a.get("rec"))
                    g["home"].setdefault("sao_rec", h.get("rec"))
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


import time as _time
RUN_START = _time.time()
COMPARE_BUDGET_SECONDS = 75        # total time for bet365/FanDuel/DraftKings prop prices
COMPARE_WINDOW_HOURS = 30          # only compare props for games starting within this window
PROPS_BROWSER_OK = {"MLB", "NBA", "NHL", "NFL"}   # leagues worth the slow browser fallback
COMPARE_DEADLINE = None


def elapsed():
    return f"{_time.time() - RUN_START:.0f}s"


def load_props():
    """Returns {league_label: [rows]} for today's props, direct first, browser if needed."""
    out, need_browser = {}, []
    for label, sao_path in LEAGUES:
        blocked = False
        try:
            rows, ncats = props.fetch_direct(get, sao_path)
            print(f"[props {label}] direct: {ncats} categories, {len(rows)} props")
        except Exception as e:
            rows, blocked = [], True
            print(f"[props {label}] direct failed: {e!r}")
        if rows:
            out[label] = rows
        elif blocked and label in PROPS_BROWSER_OK:
            need_browser.append((label, sao_path))   # only if scoresandodds refused us
    if need_browser:
        try:
            got = props.fetch_browser([sp for _, sp in need_browser], per_league_seconds=40)
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
        global COMPARE_DEADLINE
        if COMPARE_DEADLINE is None:
            COMPARE_DEADLINE = _time.time() + COMPARE_BUDGET_SECONDS
        now = datetime.now(TZ)
        soon = [r for g, r in matched
                if g["state"] == "pre" and g["start"] - now < timedelta(hours=COMPARE_WINDOW_HOURS)]
        filled, reqs = props.add_comparisons(get, soon, deadline=COMPARE_DEADLINE)
        print(f"[props {label}] sportsbook comparison: {filled} of {len(soon)} upcoming props "
              f"({reqs} game/category groups) at {elapsed()}")
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
        "ac": g.get("away_close"), "hc": g.get("home_close"),
        "aco": g.get("away_closeodds") or {}, "hco": g.get("home_closeodds") or {},
        "aop": g.get("away_openfull"), "hop": g.get("home_openfull"),
        "acl": g.get("away_closefull"), "hcl": g.get("home_closefull"),
        "cp": copy_text(label, g, weekly),
        "as": g["away"].get("score"), "hs": g["home"].get("score"),
        "url": g.get("url"),
        "day_url": f"https://www.scoresandodds.com/{sao_path}?date={g['start'].date().isoformat()}",
        "am": mascot(label, g["away"]), "hm": mascot(label, g["home"]),
        "acf": team_info(label, g["away"]), "hcf": team_info(label, g["home"]),
        "a": g["away"]["full"], "ap": g["away"]["pitcher"] if label == "MLB" else None,
        "h": g["home"]["full"], "hp": g["home"]["pitcher"] if label == "MLB" else None,
        "state": g["state"], "detail": g["detail"],
        "props": g.get("props") or {},
        "note": " · ".join(filter(None, [
            g.get("extra"),
            f"Week {g['week']}" if weekly and g.get("week") is not None else None,
            f"Neutral Site: {g['city']}" if g["neutral"] and g["city"] else None,
        ])),
    }


# ---------------------------------------------------------------- closing lines
# scoresandodds drops the "Open" column once a game starts and switches to live odds, and
# never shows a closing line. Each run publishes lines.json (the last pregame line seen for
# every game); the next run reads it back, so started games can show their closing line.

import os

LINES_KEEP_DAYS = 10
PREV_LINES = {}
NEW_LINES = {}


def lines_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "dirklaser/game-board")
    owner, _, name = repo.partition("/")
    return f"https://{owner.lower()}.github.io/{name}/lines.json"


def load_prev_lines():
    try:
        data = get(lines_url(), plain=True, params={"t": int(datetime.now().timestamp())}).json()
        PREV_LINES.update(data.get("games", {}))
        print(f"[lines] loaded {len(PREV_LINES)} remembered lines")
    except Exception as e:
        print(f"[lines] no remembered lines yet ({e!r})")


def line_key(label, g):
    return f"{label}|{g['start'].date().isoformat()}|{g['away']['full']}|{g['home']['full']}"


def market_of(text):
    """Which market an Open cell shows: total ('o8.5 -110'), spread ('-1.5 +130') or ML ('-146')."""
    t = (text or "").strip()
    if not t:
        return None
    if t[0] in "ou" and t[1:2].replace(".", "").isdigit():
        return "tot"
    return "line" if len(t.split()) >= 2 else "ml"


SIDE_MARKET = {"MLB": "ml", "NHL": "ml", "NBA": "line", "NCAAB": "line", "NFL": "line", "NCAAF": "line"}


def _num(text):
    return (text or "").split()[0] if text else ""


def mirror_total(text):
    """'o47.5 -110' -> 'u47.5' (the other side's number; price unknown)."""
    n = _num(text)
    if not n or n[0] not in "ou":
        return None
    return ("u" if n[0] == "o" else "o") + n[1:]


def mirror_spread(text):
    """'-6.5 -110' -> '+6.5' (the other side's number; price unknown)."""
    n = _num(text)
    if not n:
        return None
    if n.lower() in ("pk", "pick", "0", "+0", "-0"):
        return "PK"
    if n[0] in "+-":
        return ("+" if n[0] == "-" else "-") + n[1:]
    return None


def with_price(number, first_val):
    """Use the first-seen line's price when its number matches the mirrored number."""
    if number and first_val and _num(first_val).lower() == number.lower():
        return first_val
    return number


def full_open(label, g, first):
    """Opening side + total for both teams. scoresandodds shows one opening number per team
    (the favorite's line on one row, the total on the other); the rest is mirrored from it,
    or taken from the first line the board ever saw for this game."""
    side_mk = SIDE_MARKET.get(label, "line")
    opens = {"away": (g.get("away_odds") or {}).get("open") or (g.get("away_closeodds") or {}).get("open"),
             "home": (g.get("home_odds") or {}).get("open") or (g.get("home_closeodds") or {}).get("open")}
    first = first or {}
    out = {}
    for side, other in (("away", "home"), ("home", "away")):
        own, oth = opens[side], opens[other]
        fs = first.get("ao" if side == "away" else "ho") or {}
        # side (spread or moneyline)
        if market_of(own) == side_mk:
            sv = own
        elif side_mk == "line" and market_of(oth) == "line":
            sv = with_price(mirror_spread(oth), fs.get("line"))
        else:
            sv = fs.get(side_mk)
        # total
        if market_of(own) == "tot":
            tv = own
        elif market_of(oth) == "tot":
            mt = mirror_total(oth)
            tv = with_price(mt, fs.get("tot"))
        else:
            tv = fs.get("tot")
        out[side] = [sv, tv]
    return out


def apply_closing_lines(label, games):
    """Remember pregame lines (plus the first line ever seen); give started games their
    full closing line, and every game a full opening line for both teams."""
    filled = 0
    side_mk = SIDE_MARKET.get(label, "line")
    for g in games:
        key = line_key(label, g)
        ao, ho = g.get("away_odds") or {}, g.get("home_odds") or {}
        has_lines = any(ao.get(k) or ho.get(k) for k in ("ml", "tot", "line"))
        prev = PREV_LINES.get(key) or {}
        first = prev.get("first")
        if g["state"] == "pre":
            if has_lines:
                first = first or {"ao": ao, "ho": ho}
                NEW_LINES[key] = {"ao": ao, "ho": ho, "first": first}
            elif prev:
                NEW_LINES.setdefault(key, prev)
        elif prev:
            NEW_LINES.setdefault(key, prev)   # keep it for the Yesterday / Last week tab
            pa, ph = prev.get("ao") or {}, prev.get("ho") or {}
            g["away_closeodds"], g["home_closeodds"] = pa, ph
            g["away_closefull"] = [pa.get(side_mk), pa.get("tot")]
            g["home_closefull"] = [ph.get(side_mk), ph.get("tot")]
            if any(g["away_closefull"] + g["home_closefull"]):
                filled += 1
        op = full_open(label, g, first)
        g["away_openfull"], g["home_openfull"] = op["away"], op["home"]
    return filled


def save_lines(today):
    cutoff = (today - timedelta(days=LINES_KEEP_DAYS)).isoformat()
    keep = {k: v for k, v in NEW_LINES.items() if k.split("|")[1] >= cutoff}
    for k, v in PREV_LINES.items():          # games no longer on any tab, still recent
        if k not in keep and k.split("|")[1] >= cutoff:
            keep[k] = v
    Path("site/lines.json").write_text(json.dumps({"games": keep}), encoding="utf-8")
    print(f"[lines] saved {len(keep)} lines")


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
        try:
            closed = apply_closing_lines(label, games)
            if closed:
                print(f"[{tag}] closing lines shown for {closed} started games")
        except Exception as e:
            print(f"[{tag}] closing lines failed: {e!r}")
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
    load_prev_lines()
    try:
        todays_props = load_props()
    except Exception as e:
        print(f"[props] failed: {e!r}")
        todays_props = {}

    print(f"[timing] props loaded at {elapsed()}")
    current = build_view("current", "Today / This Week", today, today, todays_props, notes)
    print(f"[timing] current view built at {elapsed()}")
    previous = build_view("previous", "Yesterday / Last Week", today - timedelta(days=1),
                          today - timedelta(days=7), {}, notes)
    upcoming = build_view("next", "Tomorrow / Next Week", today + timedelta(days=1),
                          today + timedelta(days=7), {}, notes)
    print(f"[timing] all views built at {elapsed()}")
    # "Today": every league, today's games only (taken from the current view)
    today_only = {
        "key": "today", "label": "Today",
        "long": today.strftime("%A, %B %-d"),
        "games": [dict(g, dl=None) for g in current["games"]
                  if g["t"][:10] == today.isoformat()],
    }
    views = [previous, today_only, current, upcoming]
    payload = {
        "start_tab": 1,   # open on "Today"
        "updated": now.strftime("%-I:%M %p %Z, %a %-m/%-d"),
        "updated_iso": now.isoformat(),
        "tz": now.strftime("%Z"),   # CDT or CST
        "days": views,
        "notes": sorted(set(notes)),
    }
    template = Path(__file__).with_name("board_template.html").read_text(encoding="utf-8")
    page = template.replace("__DATA__", json.dumps(payload).replace("</", "<\\/"))
    Path("site").mkdir(exist_ok=True)
    Path("site/index.html").write_text(page, encoding="utf-8")
    try:
        save_lines(today)
    except Exception as e:
        print(f"[lines] save failed: {e!r}")
    total = len(current["games"]) + len(previous["games"]) + len(upcoming["games"])
    print(f"Wrote site/index.html with {total} games at {payload['updated']} (run took {elapsed()})")


if __name__ == "__main__":
    try:
        build()
    except Exception as e:
        print(f"Build failed: {e}")
        sys.exit(1)
