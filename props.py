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
        info = row.select_one(".table-list-props")
        chassis = info.get("data-content", "") if info else ""
        ev = re.search(r'data-event="([^"]+)"', chassis)
        mk = re.search(r'data-market="([^"]+)"', chassis)
        pid = re.search(r"/prop-bets/(\d+)/", a.get("href", "")) if a else None
        out.append({
            "cat": category, "player": player,
            "team": m.group(1), "opp": m.group(3), "home": m.group(2) == "vs",
            "prices": prices[:2],
            "event": ev.group(1) if ev else None,
            "market": mk.group(1) if mk else None,
            "pid": int(pid.group(1)) if pid else None,
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


# ---------------------------------------------------------------- per-sportsbook comparison
# The "Odds Compare" drawer on scoresandodds loads from this feed.
COMPARE_URL = "https://rga51lus77.execute-api.us-east-1.amazonaws.com/prod/market-comparison"
COMPARE_BOOKS = ("bet365", "fanduel", "draftkings")


def _valid(american):
    """Real American odds are +100 or higher, or -100 or lower. 0/None mean suspended."""
    try:
        return abs(float(american)) >= 100
    except (TypeError, ValueError):
        return False


def _payout(american):
    a = float(american)
    return 1 + (a / 100 if a > 0 else 100 / -a)


def _book_lines(market):
    """{book: {"v": line, "o": over, "u": under}} for our books, available lines only."""
    out = {}
    for book, c in (market.get("comparison") or {}).items():
        if book not in COMPARE_BOOKS or not c.get("available", True):
            continue
        over = c.get("over") if _valid(c.get("over")) else None
        under = c.get("under") if _valid(c.get("under")) else None
        if over is None and under is None:
            continue
        out[book] = {"v": c.get("value"), "o": over, "u": under}
    return out


def best_books(books):
    """Best over = lowest line, then best price. Best under = highest line, then best price."""
    overs = [(b, x) for b, x in books.items() if x.get("o") is not None]
    unders = [(b, x) for b, x in books.items() if x.get("u") is not None]
    bo = min(overs, key=lambda t: ((t[1]["v"] if t[1]["v"] is not None else 0), -_payout(t[1]["o"])))[0] if overs else None
    bu = max(unders, key=lambda t: ((t[1]["v"] if t[1]["v"] is not None else 0), _payout(t[1]["u"])))[0] if unders else None
    return bo, bu


def add_comparisons(get, rows, workers=8):
    """Fill row["books"] for rows. One request per game+category; falls back to
    per-player requests if the feed only returns the filtered player."""
    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    groups = {}
    for r in rows:
        if r.get("event") and r.get("market"):
            groups.setdefault((r["event"], r["market"]), []).append(r)
    if not groups:
        return 0, 0
    hdrs = {"Referer": BASE + "/", "Origin": BASE}

    def call(event, market, player=None):
        params = {"event": event, "market": market, "t": f"{_t.time():.3f}"}
        if player:
            params["filter"] = player
        try:
            return get(COMPARE_URL, extra_headers=hdrs, params=params).json().get("markets") or []
        except Exception:
            return None

    def do_group(key):
        event, market = key
        members = groups[key]
        found = call(event, market)
        by_id = {}
        if found:
            for m in found:
                pid = (m.get("player") or {}).get("id")
                if pid is not None:
                    by_id[pid] = m
        missing = [r for r in members if r.get("pid") not in by_id]
        if missing and len(by_id) <= 1:
            for r in missing[:40]:  # feed needs one request per player
                one = call(event, market, r["player"])
                for m in one or []:
                    pid = (m.get("player") or {}).get("id")
                    if pid is not None:
                        by_id[pid] = m
        n = 0
        for r in members:
            m = by_id.get(r.get("pid"))
            if m:
                books = _book_lines(m)
                if books:
                    r["books"] = books
                    n += 1
        return n

    with ThreadPoolExecutor(max_workers=workers) as ex:
        filled = sum(ex.map(do_group, list(groups)))
    return filled, len(groups)
