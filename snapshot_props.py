#!/usr/bin/env python3
"""
One-time snapshot tool, round 3: why does the board see only some college basketball games?

Saves into snapshot/:
  plain.html       the scoresandodds NCAAB page exactly as the board receives it
  browser.html     the same page in a real browser after scrolling to the bottom
  browser.png      screenshot of the browser view
  requests.json    every request the page made while loading and scrolling
  bodies.json      the contents of scoresandodds' own data requests
  summary.txt      quick counts (games found each way, whether Coppin State appears)
"""
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import urllib.request
from playwright.async_api import async_playwright

OUT = Path("snapshot")
OUT.mkdir(exist_ok=True)
DAY = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
URL = f"https://www.scoresandodds.com/ncaab?date={DAY}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
AD_HINTS = ("doubleclick", "googletag", "google-analytics", "facebook", "taboola", "criteo",
            "aditude", "prebid", "amazon-adsystem", "cookielaw", "onetrust", "clarity",
            "snapchat", "twitter", "id5", "optable", "newsroom", "rudder", "scorecard",
            "admedo", "crwdcntrl", "liftdsp", "rtmark", "kueez", "33across", "adsrvr",
            "bet-links", "marfeel", "mrf.io", "fastclick", "gstatic", "googleapis")
SUMMARY = []


def note(msg):
    print(msg, flush=True)
    SUMMARY.append(msg)
    (OUT / "summary.txt").write_text("\n".join(SUMMARY), encoding="utf-8")


def count_games(html):
    rots = re.findall(r"<t[dh][^>]*>\s*(?:<[^>]+>\s*)*(\d{3,4})\b", html)
    return len(set(rots)), ("Coppin" in html)


async def browser_pass():
    reqs, bodies, tasks = [], [], []
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(user_agent=UA, viewport={"width": 1400, "height": 1000},
                                  locale="en-US", timezone_id="America/Chicago")
        page = await ctx.new_page()
        phase = {"now": "load"}

        async def grab(resp):
            try:
                r = resp.request
                if r.resource_type in ("image", "font", "media", "stylesheet"):
                    return
                if any(h in resp.url for h in AD_HINTS):
                    return
                reqs.append({"phase": phase["now"], "type": r.resource_type, "method": r.method,
                             "status": resp.status, "url": resp.url})
                if r.resource_type in ("xhr", "fetch") or "scoresandodds" in resp.url:
                    body = await asyncio.wait_for(resp.text(), 8)
                    bodies.append({"phase": phase["now"], "url": resp.url, "body": body[:400000]})
            except Exception:
                pass

        page.on("response", lambda r: tasks.append(asyncio.ensure_future(grab(r))))
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            note(f"page load issue: {e!r}")
        await page.wait_for_timeout(8000)
        n0, c0 = count_games(await page.content())
        note(f"browser right after load: {n0} rotation numbers, Coppin State on page: {c0}")
        phase["now"] = "scroll"
        for i in range(25):
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(900)
        await page.wait_for_timeout(4000)
        pending = [t for t in tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=8)
        html = await page.content()
        (OUT / "browser.html").write_text(html, encoding="utf-8")
        try:
            await page.screenshot(path=str(OUT / "browser.png"), full_page=False, timeout=20000)
        except Exception:
            pass
        n1, c1 = count_games(html)
        note(f"browser after scrolling: {n1} rotation numbers, Coppin State on page: {c1}")
        try:
            await asyncio.wait_for(b.close(), 20)
        except Exception:
            pass
    (OUT / "requests.json").write_text(json.dumps(reqs, indent=1), encoding="utf-8")
    (OUT / "bodies.json").write_text(json.dumps(bodies, indent=1), encoding="utf-8")
    scroll_reqs = [r for r in reqs if r["phase"] == "scroll"]
    note(f"requests made while scrolling: {len(scroll_reqs)}")
    for r in scroll_reqs[:20]:
        note(f"   {r['method']} {r['status']} {r['type']} {r['url'][:200]}")


def main():
    note(f"page: {URL}")
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
        (OUT / "plain.html").write_text(text, encoding="utf-8")
        n, c = count_games(text)
        note(f"plain request (what the board sees): status {status}, {len(text)} bytes, "
             f"{n} rotation numbers, Coppin State on page: {c}")
    except Exception as e:
        note(f"plain request failed: {e!r}")
    try:
        asyncio.run(asyncio.wait_for(browser_pass(), 240))
    except Exception as e:
        note(f"browser pass failed: {e!r}")
    note("done")


if __name__ == "__main__":
    main()
