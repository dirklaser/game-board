"""
Player props from scoresandodds.com.

Each league's props page lists categories (Strikeouts, Hits, Shots on Goal, ...), each
loaded from its own address such as /mlb/props/strikeouts. This module:
  1. tries to read each category directly (fast, no browser), and
  2. if that returns nothing, opens the props page in a headless browser (Playwright),
     clicks through every category, and reads what loads.
"""
import re
import time

from bs4 import BeautifulSoup

BASE = "https://www.scoresandodds.com"

# Abbreviation differences between the league feeds and scoresandodds
ALIASES = {
    "MLB": {"ARI": "AZ", "OAK": "ATH", "SAC": "ATH", "WAS": "WSH", "WSN": "WSH", "CHW": "CWS",
            "SDP": "SD", "SFG": "SF", "KCR": "KC", "TBR": "TB", "LA": "LAD"},
    "NHL": {"LA": "LAK", "NJ": "NJD", "SJ": "SJS", "TB": "TBL", "UTAH": "UTA", "MON": "MTL",
            "VEG": "VGK", "WAS": "WSH", "CLB": "CBJ", "NAS": "NSH"},
    "NFL": {"WAS": "WSH", "JAC": "JAX", "LA": "LAR", "ARZ": "ARI", "BLT": "BAL", "CLV": "CLE",
            "HST": "HOU"},
}


def canon(league, abbr):
    a = (abbr or "").upper().strip()
    return ALIASES.get(league, {}).get(a, a)


def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def categories(html):
    soup = BeautifulSoup(html, "html.parser")
    ul = soup.find(id="prop-options")
    if not ul:
        return []
    return [(li.get("data-endpoint"), _text(li)) for li in ul.find_all("li") if li.get("data-endpoint")]


def parse_rows(html, category):
    """Parse prop rows out of a props page or table fragment."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("div.table-list-row"):
        name_box = row.select_one(".props-name")
        if not name_box:
            continue
        a = name_box.find("a")
        player = _text(a) if a else ""
        matchup = _text(name_box.find("span"))
        m = re.match(r"([A-Z]{2,4})\s*(vs|@)\s*([A-Z]{2,4})", matchup)
        if not player or not m:
            continue
        prices = []
        for box in row.select(".best-odds-container"):
            line = _text(box.select_one(".data-moneyline"))
            odds = _text(box.select_one(".data-odds"))
            img = box.select_one(".book-icn img")
            book = img.get("alt", "") if img else ""
            if line or odds:
                prices.append({"line": line, "odds": odds, "book": book})
        if not prices:
            continue
        out.append({
            "cat": category, "player": player,
            "team": m.group(1), "opp": m.group(3), "home": m.group(2) == "vs",
            "prices": prices[:2],
        })
    return out


def fetch_direct(get, league_path):
    """Try reading every category with plain requests. Returns (rows, categories_found)."""
    page = get(f"{BASE}/{league_path}/props").text
    cats = categories(page)
    rows = []
    for endpoint, label in cats:
        for headers in ({"X-Requested-With": "XMLHttpRequest",
                         "Referer": f"{BASE}/{league_path}/props"}, {}):
            try:
                html = get(BASE + endpoint, extra_headers=headers).text
            except Exception:
                continue
            got = parse_rows(html, label)
            if got:
                rows.extend(got)
                break
    return rows, len(cats)


def fetch_browser(league_paths, per_league_seconds=150):
    """Headless-browser fallback. Returns {league_path: rows}."""
    from playwright.sync_api import sync_playwright  # only needed if direct reading fails

    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            viewport={"width": 1400, "height": 1000}, locale="en-US",
            timezone_id="America/Chicago",
        )
        ctx.set_default_timeout(15000)
        for lp in league_paths:
            rows, start = [], time.time()
            page = ctx.new_page()
            try:
                page.goto(f"{BASE}/{lp}/props", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                cats = categories(page.content())
                for i, (endpoint, label) in enumerate(cats):
                    if time.time() - start > per_league_seconds:
                        print(f"  [{lp} props] time limit reached after {i} categories")
                        break
                    if i > 0:
                        item = page.locator(f'#prop-options li[data-endpoint="{endpoint}"]')
                        try:
                            opener = page.locator("#prop-options").locator("xpath=..")
                            if not item.is_visible():
                                opener.click(timeout=5000)
                                page.wait_for_timeout(500)
                            item.click(timeout=5000)
                        except Exception:
                            item.dispatch_event("click")
                        page.wait_for_timeout(3500)
                    table = page.locator("#prop-table")
                    html = table.inner_html(timeout=10000) if table.count() else page.content()
                    rows.extend(parse_rows(html, label))
            except Exception as e:
                print(f"  [{lp} props] browser error: {e!r}")
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            results[lp] = rows
        browser.close()
    return results
