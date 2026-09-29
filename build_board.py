#!/usr/bin/env python3
"""
Game Board builder.

Builds site/index.html with today's and tomorrow's MLB, NFL and NHL games:
  - start times, teams, pitchers, neutral sites and live status from ESPN's public scoreboard feed
  - ROT numbers (and MLB pitcher handedness) from scoresandodds.com

Run by GitHub Actions on a schedule. Every run prints a short summary to the log,
so if something stops matching you can see which league/day it was.
"""
import html
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

# (label, ESPN path, scoresandodds path)
LEAGUES = [
    ("MLB", "baseball/mlb", "mlb"),
    ("NFL", "football/nfl", "nfl"),
    ("NHL", "hockey/nhl", "nhl"),
]

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


def get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=25, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- ESPN (times, teams)

def espn_games(espn_path, day):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{espn_path}/scoreboard"
    data = get(url, params={"dates": day.strftime("%Y%m%d")}).json()
    week = (data.get("week") or {}).get("number")
    games = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(TZ)
        if start.date() != day:
            continue
        side = {}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            full = t.get("displayName") or t.get("name") or "?"
            probables = c.get("probables") or []
            pitcher = probables[0].get("athlete", {}).get("displayName") if probables else None
            side[c.get("homeAway")] = {
                "full": NAME_OVERRIDES.get(full, full),
                "nick": t.get("name") or t.get("shortDisplayName") or "",
                "pitcher": pitcher,
            }
        if "home" not in side or "away" not in side:
            continue
        status = ev.get("status", {}).get("type", {})
        venue = comp.get("venue", {}) or {}
        games.append({
            "start": start,
            "away": side["away"],
            "home": side["home"],
            "state": status.get("state", "pre"),          # pre / in / post
            "detail": status.get("shortDetail", ""),
            "neutral": bool(comp.get("neutralSite")),
            "city": (venue.get("address") or {}).get("city"),
            "week": week,
        })
    return games


# ---------------------------------------------------------------- scoresandodds (ROT numbers)

PITCHER_RE = re.compile(r"([A-Z][^()\d]*?\s*\([LR]\))")


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
        rows.append({
            "rot": rot,
            "team": team_re.search(a["href"]).group(1),
            "pitcher": re.sub(r"\s+", " ", pm.group(1)).strip() if pm else None,
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
        for label, espn_path, sao_path in LEAGUES:
            try:
                games = espn_games(espn_path, day)
            except Exception as e:
                print(f"[{day} {label}] ESPN failed: {e}")
                notes.append(f"{label} schedule for {day:%-m/%-d} couldn't be loaded this run.")
                continue
            if not games:
                print(f"[{day} {label}] no games")
                continue
            try:
                pairs = sao_pairs(sao_path, day)
                matched = attach_rots(games, pairs)
                print(f"[{day} {label}] {len(games)} games, {len(pairs)} ROT pairs, {matched} matched")
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
                    "a": g["away"]["full"], "ap": g["away"]["pitcher"] if label == "MLB" else None,
                    "h": g["home"]["full"], "hp": g["home"]["pitcher"] if label == "MLB" else None,
                    "state": g["state"], "detail": g["detail"],
                    "note": " ".join(filter(None, [
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
