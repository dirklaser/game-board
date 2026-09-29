#!/usr/bin/env python3
"""
One-time snapshot tool, round 2: find where "Odds Compare" gets per-sportsbook prop odds.

For MLB and NHL props pages it:
  - records every request the page makes after load (address, type, status)
  - clicks the first "Odds Compare" button and saves what loads into the drawer
  - saves the bodies of the site's own data requests
  - saves a copy of the site's main script (global.js)
Everything goes into snapshot/.
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("snapshot")
OUT.mkdir(exist_ok=True)
LOG = []
SKIP_TYPES = {"image", "font", "media", "stylesheet"}
AD_HINTS = ("doubleclick", "googletag", "google-analytics", "facebook", "taboola", "criteo",
            "aditude", "prebid", "amazon-adsystem", "cookielaw", "onetrust", "clarity",
            "snapchat", "twitter", "id5", "optable", "newsroom", "rudder", "scorecard",
            "admedo", "crwdcntrl", "liftdsp", "rtmark", "kueez", "33across", "adsrvr",
            "bet-links", "marfeel", "mrf.io", "fastclick")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)
    (OUT / "log.txt").write_text("\n".join(LOG), encoding="utf-8")


async def visit(ctx, name, url):
    page = await ctx.new_page()
    requests_seen, bodies, tasks = [], [], []
    phase = {"now": "load"}

    async def grab(resp):
        try:
            req = resp.request
            if req.resource_type in SKIP_TYPES or any(h in resp.url for h in AD_HINTS):
                return
            requests_seen.append({"phase": phase["now"], "type": req.resource_type,
                                  "method": req.method, "status": resp.status, "url": resp.url,
                                  "post": (req.post_data or "")[:2000]})
            if req.resource_type in ("xhr", "fetch", "document", "script"):
                body = await asyncio.wait_for(resp.text(), 8)
                bodies.append({"phase": phase["now"], "url": resp.url, "type": req.resource_type,
                               "body": body[:600000]})
        except Exception:
            pass

    page.on("response", lambda r: tasks.append(asyncio.ensure_future(grab(r))))

    async def settle(secs=8):
        pending = [t for t in tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=secs)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        log(f"{name}: page load issue: {e!r}")
    await page.wait_for_timeout(10000)
    await settle()
    log(f"{name}: loaded, {len(requests_seen)} site requests so far")

    phase["now"] = "compare-click"
    try:
        btn = page.locator("button.table-list-button").first
        await btn.scroll_into_view_if_needed(timeout=10000)
        await btn.click(timeout=10000)
        await page.wait_for_timeout(7000)
        await settle()
        target = await btn.get_attribute("data-content")
        drawer = page.locator(target) if target else None
        html = await drawer.inner_html(timeout=10000) if drawer is not None else ""
        (OUT / f"{name}_drawer.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT / f"{name}_drawer.png"), full_page=False, timeout=20000)
        log(f"{name}: opened Odds Compare ({target}), drawer html {len(html)} chars")
    except Exception as e:
        log(f"{name}: Odds Compare click failed: {e!r}")

    (OUT / f"{name}_requests.json").write_text(json.dumps(requests_seen, indent=1), encoding="utf-8")
    (OUT / f"{name}_bodies.json").write_text(json.dumps(bodies, indent=1), encoding="utf-8")
    clicks = [r for r in requests_seen if r["phase"] == "compare-click"]
    log(f"{name}: {len(clicks)} requests after clicking Odds Compare")
    for r in clicks[:15]:
        log(f"   {r['method']} {r['status']} {r['type']} {r['url'][:180]}")
    try:
        await asyncio.wait_for(page.close(), 10)
    except Exception:
        pass


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 1000},
                                        locale="en-US", timezone_id="America/Chicago")
        for name, url in [("mlb_props", "https://www.scoresandodds.com/mlb/props"),
                          ("nhl_props", "https://www.scoresandodds.com/nhl/props")]:
            log(f"{name}: opening {url}")
            try:
                await asyncio.wait_for(visit(ctx, name, url), 150)
            except asyncio.TimeoutError:
                log(f"{name}: gave up after 150s")
            except Exception as e:
                log(f"{name}: failed: {e!r}")
        try:
            await asyncio.wait_for(browser.close(), 20)
        except Exception:
            pass
    log("done")


if __name__ == "__main__":
    asyncio.run(main())
