#!/usr/bin/env python3
"""
One-time snapshot tool (not part of the regular board build).

Opens scoresandodds pages in a real headless browser, lets the site's own code load,
and saves into snapshot/:
  - <name>.html      the fully loaded page
  - <name>.png       a screenshot
  - <name>.json      every data feed (JSON) the page requested, with its address
  - log.txt          what happened at each step
"""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("snapshot")
OUT.mkdir(exist_ok=True)
LOG = []

PAGES = [
    ("mlb_props", "https://www.scoresandodds.com/mlb/props", False),
    ("nhl_props", "https://www.scoresandodds.com/nhl/props", False),
    ("nfl_props", "https://www.scoresandodds.com/nfl/props", False),
    ("mlb_day", "https://www.scoresandodds.com/mlb", True),
    ("nhl_day", "https://www.scoresandodds.com/nhl", True),
]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def log(msg):
    print(msg)
    LOG.append(msg)


async def save(page, name, feeds):
    try:
        (OUT / f"{name}.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
    except Exception as e:
        log(f"{name}: save failed: {e}")
    (OUT / f"{name}.json").write_text(json.dumps(feeds, indent=1), encoding="utf-8")
    log(f"{name}: saved ({len(feeds)} data feeds so far)")


async def visit(ctx, name, url, open_details):
    page = await ctx.new_page()
    feeds, tasks = [], []

    async def grab(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await resp.text()
            feeds.append({"url": resp.url, "status": resp.status, "body": body[:400000]})
        except Exception:
            pass

    page.on("response", lambda r: tasks.append(asyncio.ensure_future(grab(r))))

    try:
        await page.goto(url, wait_until="networkidle", timeout=60000)
    except Exception as e:
        log(f"{name}: page load issue: {e}")
    await page.wait_for_timeout(6000)
    await asyncio.gather(*tasks, return_exceptions=True)
    await save(page, name, feeds)

    if open_details:
        # Open the first game's "Game Details" panel, then its "Props" tab
        try:
            await page.get_by_text("Game Details").first.click(timeout=10000)
            await page.wait_for_timeout(4000)
            await asyncio.gather(*tasks, return_exceptions=True)
            await save(page, f"{name}_details", feeds)
        except Exception as e:
            log(f"{name}: couldn't open Game Details: {e}")
        try:
            tab = page.locator(
                "xpath=//*[normalize-space(text())='Props' and not(ancestor-or-self::a[@href])]"
            )
            n = await tab.count()
            log(f"{name}: found {n} 'Props' tab candidates")
            clicked = False
            for i in range(n):
                el = tab.nth(i)
                if await el.is_visible():
                    await el.click(timeout=5000)
                    clicked = True
                    break
            if clicked:
                await page.wait_for_timeout(5000)
                await asyncio.gather(*tasks, return_exceptions=True)
                await save(page, f"{name}_props_tab", feeds)
            else:
                log(f"{name}: no visible Props tab to click")
        except Exception as e:
            log(f"{name}: couldn't open Props tab: {e}")

    await page.close()


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(
            user_agent=UA, viewport={"width": 1400, "height": 1000},
            locale="en-US", timezone_id="America/Chicago",
        )
        for name, url, details in PAGES:
            try:
                await visit(ctx, name, url, details)
            except Exception as e:
                log(f"{name}: failed: {e}")
        await browser.close()
    (OUT / "log.txt").write_text("\n".join(LOG), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
