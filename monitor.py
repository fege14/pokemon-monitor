import os
import random
import re
import signal
import sys
import time

import requests
from playwright.sync_api import sync_playwright

# Resource types we don't need for reading the count text. Blocking these
# cuts page weight ~80% and trims each cycle by several seconds.
_BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}
# Random ± seconds added to each site's next-check time so polling is not
# perfectly periodic (less obvious as automation to rate-limiters).
_JITTER_SECONDS = 3
# Long-running Playwright sessions degrade — sockets pile up, the route
# handler queue wedges, runner egress gets throttled. Recycling the browser
# every 30min reliably clears all of that.
_BROWSER_RECYCLE_SECONDS = 30 * 60

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# Each site declares its CSS selector, a regex (group 1 = the count number),
# and how often (seconds) it should be polled.
SITES = [
    {
        "name": "Bilka",
        "url": "https://www.bilka.dk/brands/pokemon/pokemon-kort/pl/pokemon-kort/",
        "selector": "div.count.flex",
        "regex": r"(\d[\d.]*)\s+produkter",
        "unit": "produkter",
        "interval": 20,
        "items_root": "#products-row",
    },
    {
        "name": "BR",
        "url": "https://www.br.dk/maerker/o-aa/pokemon/pokemon-kort/pl/pokemon-kort/?p=0",
        "selector": "div.count.flex",
        "regex": r"(\d[\d.]*)\s+produkter",
        "unit": "produkter",
        "interval": 20,
        "items_root": "#products-row",
    },
    {
        "name": "Foetex",
        "url": "https://www.foetex.dk/brands/pokemon/pokemon-kort/pl/pokemon-kort/?p=0",
        "selector": "div.count.flex",
        "regex": r"(\d[\d.]*)\s+varer",
        "unit": "varer",
        "interval": 20,
        "items_root": "#products-row",
    },
]

# Optional dev override — set POLL_SECONDS=2 locally to make every site fast.
_override = os.environ.get("POLL_SECONDS")
if _override:
    for _s in SITES:
        _s["interval"] = int(_override)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(title, message, priority="high", tags="bell"):
    if not NTFY_TOPIC:
        log(f"NOTIFY (dry-run) | {title} | {message}")
        return
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": tags,
            },
            timeout=10,
        )
    except Exception as e:
        log(f"notify error: {e}")


def dismiss_consent(page):
    for sel in (
        "button:has-text('Accepter alle')",
        "button:has-text('Tillad alle')",
        "button:has-text('Accepter')",
        "#coiPage-1 .coi-banner__accept",
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=2000)
                return True
        except Exception:
            pass
    return False


_FIND_TEXT_JS = """
([sel, regexStr]) => {
    const re = new RegExp(regexStr, 'i');
    for (const el of document.querySelectorAll(sel)) {
        const t = (el.innerText || '').trim();
        if (re.test(t)) return t;
    }
    return null;
}
"""

# Walks the product grid and returns [{id, url, name}] for each card. Title
# selector falls back across the two Salling layouts (Bilka/BR vs Foetex).
_EXTRACT_ITEMS_JS = """
(rootSel) => {
    const root = document.querySelector(rootSel);
    if (!root) return [];
    const out = [];
    for (const card of root.querySelectorAll('div[id^="product-"]')) {
        const link = card.querySelector('a[href]');
        const titleEl = card.querySelector('.v-card__title, .product-card__title');
        out.push({
            id: card.id,
            url: link ? link.href : '',
            name: titleEl ? (titleEl.innerText || '').trim() : '',
        });
    }
    return out;
}
"""


def read_state(page, site):
    page.goto(site["url"], wait_until="domcontentloaded", timeout=30_000)
    dismiss_consent(page)
    page.wait_for_function(
        "([sel, regexStr]) => {"
        "  const re = new RegExp(regexStr, 'i');"
        "  for (const el of document.querySelectorAll(sel)) {"
        "    if (re.test((el.innerText || '').trim())) return true;"
        "  }"
        "  return false;"
        "}",
        arg=[site["selector"], site["regex"]],
        timeout=30_000,
    )
    text = page.evaluate(_FIND_TEXT_JS, [site["selector"], site["regex"]])
    if not text:
        raise RuntimeError(f"selector matched nothing for {site['name']}")
    m = re.search(site["regex"], text, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"regex did not match in {text!r}")
    count = int(m.group(1).replace(".", ""))

    items = None
    if site.get("items_root"):
        page.wait_for_selector(
            f'{site["items_root"]} div[id^="product-"]',
            timeout=30_000,
        )
        items = page.evaluate(_EXTRACT_ITEMS_JS, site["items_root"])
    return {"count": count, "items": items}


def main():
    if not NTFY_TOPIC:
        log("WARN: NTFY_TOPIC not set — running in dry-run mode")

    last_count = {s["name"]: None for s in SITES}
    last_items = {s["name"]: None for s in SITES}
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    def _route(route):
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()

    with sync_playwright() as pw:
        def _new_browser():
            br = pw.chromium.launch(headless=True)
            c = br.new_context(
                user_agent=UA,
                locale="da-DK",
                viewport={"width": 1366, "height": 900},
            )
            p = c.new_page()
            p.route("**/*", _route)
            return br, c, p

        browser, ctx, page = _new_browser()
        browser_started = time.time()

        intervals = " ".join(f"{s['name']}:{s['interval']}s" for s in SITES)
        log(f"started | sites={len(SITES)} | {intervals}")
        notify(
            "Monitor started",
            f"Polling {len(SITES)} sites ({intervals})",
            priority="low",
        )

        next_check = {s["name"]: 0.0 for s in SITES}
        while not stop["flag"]:
            if time.time() - browser_started > _BROWSER_RECYCLE_SECONDS:
                log("recycling browser")
                try:
                    browser.close()
                except Exception as e:
                    log(f"browser close error: {e}")
                browser, ctx, page = _new_browser()
                browser_started = time.time()

            for site in SITES:
                now = time.time()
                if now < next_check[site["name"]]:
                    continue
                try:
                    state = read_state(page, site)
                except Exception as e:
                    log(f"{site['name']}: error: {e}")
                    next_check[site["name"]] = (
                        time.time() + site["interval"] + random.uniform(-_JITTER_SECONDS, _JITTER_SECONDS)
                    )
                    continue

                count = state["count"]
                items = state["items"]
                prev_count = last_count[site["name"]]
                prev_items = last_items[site["name"]]

                if items is not None:
                    items_by_id = {it["id"]: it for it in items}
                    if prev_items is None:
                        log(f"{site['name']}: baseline = {count} {site['unit']} ({len(items_by_id)} items indexed)")
                    else:
                        added_ids = items_by_id.keys() - prev_items.keys()
                        removed_ids = prev_items.keys() - items_by_id.keys()
                        if added_ids or removed_ids:
                            log(
                                f"{site['name']}: {prev_count} -> {count} | "
                                f"+{len(added_ids)} -{len(removed_ids)}"
                            )
                            for aid in added_ids:
                                it = items_by_id[aid]
                                name = it.get("name") or aid
                                notify(
                                    f"{site['name']} +ADDED",
                                    f"{name}\n{it.get('url', '')}",
                                )
                            for rid in removed_ids:
                                it = prev_items[rid]
                                name = it.get("name") or rid
                                notify(
                                    f"{site['name']} -REMOVED",
                                    f"{name}\n{it.get('url', '')}",
                                )
                        else:
                            log(f"{site['name']}: {count} (no change, {len(items_by_id)} items)")
                    last_items[site["name"]] = items_by_id
                else:
                    if prev_count is None:
                        log(f"{site['name']}: baseline = {count} {site['unit']}")
                    elif count != prev_count:
                        delta = count - prev_count
                        arrow = "+" if delta > 0 else ""
                        log(f"{site['name']}: {prev_count} -> {count} ({arrow}{delta})")
                        notify(
                            f"{site['name']}: {prev_count} -> {count}",
                            f"{count} {site['unit']} ({arrow}{delta})\n{site['url']}",
                        )
                    else:
                        log(f"{site['name']}: {count} (no change)")

                last_count[site["name"]] = count
                next_check[site["name"]] = (
                    time.time() + site["interval"] + random.uniform(-_JITTER_SECONDS, _JITTER_SECONDS)
                )

            if stop["flag"]:
                break
            time.sleep(1)

        browser.close()
    log("shutdown")


if __name__ == "__main__":
    sys.exit(main())
