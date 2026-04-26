import os
import re
import signal
import sys
import time

import requests
from playwright.sync_api import sync_playwright

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))

# Each site declares the CSS selector and a regex (group 1 = the count number).
# The Salling Group sites (Bilka/BR/Foetex) share the same magnolia-plp template;
# WobblyNerdles is a Danish-language WooCommerce shop.
SITES = [
    {
        "name": "Bilka",
        "url": "https://www.bilka.dk/brands/pokemon/pokemon-kort/pl/pokemon-kort/",
        "selector": "div.count.flex",
        "regex": r"(\d[\d.]*)\s+produkter",
        "unit": "produkter",
    },
    {
        "name": "BR",
        "url": "https://www.br.dk/maerker/o-aa/pokemon/pokemon-kort/pl/pokemon-kort/?p=0",
        "selector": "div.count.flex",
        "regex": r"(\d[\d.]*)\s+produkter",
        "unit": "produkter",
    },
    {
        "name": "Foetex",
        "url": "https://www.foetex.dk/brands/pokemon/pokemon-kort/pl/pokemon-kort/?p=0",
        "selector": "div.count.flex",
        "regex": r"(\d[\d.]*)\s+varer",
        "unit": "varer",
    },
    {
        "name": "WobblyNerdles",
        "url": "https://thewobblynerdles.dk/product-category/kortspil/pokemon/",
        "selector": ".woocommerce-result-count",
        "regex": r"af\s+(\d+)\s+resultater",
        "unit": "resultater",
    },
]

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


def read_count(page, site):
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
    return int(m.group(1).replace(".", ""))


def main():
    if not NTFY_TOPIC:
        log("WARN: NTFY_TOPIC not set — running in dry-run mode")

    last = {s["name"]: None for s in SITES}
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=UA,
            locale="da-DK",
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        log(f"started | sites={len(SITES)} interval={POLL_SECONDS}s")
        notify(
            "Monitor started",
            f"Polling {len(SITES)} sites every {POLL_SECONDS}s",
            priority="low",
        )

        while not stop["flag"]:
            for site in SITES:
                try:
                    count = read_count(page, site)
                except Exception as e:
                    log(f"{site['name']}: error: {e}")
                    continue

                prev = last[site["name"]]
                if prev is None:
                    log(f"{site['name']}: baseline = {count} {site['unit']}")
                elif count != prev:
                    delta = count - prev
                    arrow = "+" if delta > 0 else ""
                    log(f"{site['name']}: {prev} -> {count} ({arrow}{delta})")
                    notify(
                        f"{site['name']}: {prev} -> {count}",
                        f"{count} {site['unit']} ({arrow}{delta})\n{site['url']}",
                    )
                else:
                    log(f"{site['name']}: {count} (no change)")
                last[site["name"]] = count

            for _ in range(POLL_SECONDS):
                if stop["flag"]:
                    break
                time.sleep(1)

        browser.close()
    log("shutdown")


if __name__ == "__main__":
    sys.exit(main())
