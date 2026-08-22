#!/usr/bin/env python3
"""
Multi-site PS5 Pro stock checker for Thai retailers.
Notifies via ntfy.sh when:
  - A product comes back in stock
  - A new product listing appears
  - A product page goes missing (URL may have changed)
  - A site can't be parsed (possible layout change)

State is persisted to state.json between GitHub Actions runs.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "sony-stock-a8cebf9a48")
STATE_FILE = "state.json"
TAG_RE = re.compile(r"<[^>]+>")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
}

# ── Site definitions ──────────────────────────────────────────────────────────
# type "collection" : scrapes all product cards from a listing page (Sony store)
# type "product"    : checks a single product page
#
# out_of_stock_phrases : if ANY phrase found in page HTML → out of stock
# page_gone_phrases    : if ANY phrase found → product page no longer exists
# use_playwright       : True = headless browser (JS-rendered / bot-protected sites)

SITES = [
    # ── Simple fetch (no headless browser needed) ─────────────────────────────
    {
        "name": "Sony Store TH",
        "url": "https://store.sony.co.th/collections/console",
        "type": "collection",
        "use_playwright": False,
        "out_of_stock_phrases": ["Notify me when back in stock"],
        "page_gone_phrases": [],
    },
    {
        "name": "JIB",
        "url": (
            "https://www.jib.co.th/web/product/readProduct/79404/1652/"
            "GAME-CONSOLE--%E0%B9%80%E0%B8%84%E0%B8%A3%E0%B8%B7%E0%B9%88%E0%B8%AD%E0%B8%87"
            "%E0%B9%80%E0%B8%A5%E0%B9%88%E0%B8%99%E0%B9%80%E0%B8%81%E0%B8%A1--SONY-PLAYSTATION-5-PRO"
        ),
        "type": "product",
        "use_playwright": False,
        "out_of_stock_phrases": ["สินค้าหมดชั่วคราว", "สินค้าหมด", "หมดสต็อก", "out of stock"],
        "page_gone_phrases": ["ไม่พบสินค้า", "ไม่พบหน้านี้", "page not found"],
    },
    {
        "name": "HomePro",
        "url": "https://www.homepro.co.th/p/1276435",
        "type": "product",
        "use_playwright": True,
        "out_of_stock_phrases": ["สินค้าหมด", "สต๊อคคงเหลือ 0", "หมดสต็อก", "out of stock"],
        "page_gone_phrases": ["ไม่พบสินค้า", "page not found", "404 not found"],
    },
    {
        "name": "Advice",
        "url": "https://www.advice.co.th/branch-a001/index.php/product/productdetail/A0176904",
        "type": "product",
        "use_playwright": False,
        "out_of_stock_phrases": ["สินค้าหมด", "หมดสต็อก", "out of stock"],
        "page_gone_phrases": ["ไม่พบสินค้า", "page not found"],
    },
    # ── Playwright (headless browser) sites ───────────────────────────────────
    {
        "name": "BNN (TH) - PS5 Pro New Version",
        "url": (
            "https://www.bnn.in.th/th/p/gaming-gear/console/playstation-0/"
            "sony-playstation-5-pro-console-new-version-4948872417068_zv4q1p"
        ),
        "type": "product",
        "use_playwright": True,
        "out_of_stock_phrases": ["สินค้าหมด", "หมดสต็อก", "out of stock", "notify me", "แจ้งเตือนฉัน"],
        "page_gone_phrases": ["ไม่พบสินค้า", "page not found", "404"],
    },
    {
        "name": "BNN (EN) - PS5 Pro",
        "url": "https://www.bnn.in.th/en/p/sony-playstation-5-pro-console-4948872416313_dlejqn",
        "type": "product",
        "use_playwright": True,
        "out_of_stock_phrases": ["out of stock", "notify me", "สินค้าหมด", "หมดสต็อก"],
        "page_gone_phrases": ["page not found", "404", "ไม่พบสินค้า"],
    },
    {
        "name": "Studio7",
        "url": (
            "https://www.studio7thailand.com/th/p/"
            "sony-playstation-5-pro-console-new-version-4948872417068_zv4q1p"
        ),
        "type": "product",
        "use_playwright": True,
        "out_of_stock_phrases": ["สินค้าหมด", "หมดสต็อก", "out of stock", "notify me", "แจ้งเตือนฉัน"],
        "page_gone_phrases": ["ไม่พบสินค้า", "page not found", "404"],
    },
    # PowerBuy removed - blocked by Cloudflare even with headless browser
    {
        "name": "HappyConsole",
        "url": "https://www.happyconsole.com/product/2619/",
        "type": "product",
        "use_playwright": True,
        "long_wait": True,
        "out_of_stock_phrases": ["สินค้าหมด", "หมดสต็อก", "out of stock"],
        "page_gone_phrases": ["ไม่พบสินค้า", "page not found", "404"],
    },
    {
        "name": "AIS",
        "url": "https://www.ais.th/consumers/store/accessories/sony/sony-playstation-5-pro-console",
        "type": "product",
        "use_playwright": True,
        "long_wait": True,
        "out_of_stock_phrases": ["สินค้าหมด", "หมดสต็อก", "out of stock", "สินค้าหมดชั่วคราว"],
        "page_gone_phrases": ["ไม่พบสินค้า", "page not found", "404"],
    },
]

# ── Fetching ──────────────────────────────────────────────────────────────────

def fetch_simple(url):
    """Returns (status, html, final_url). Raises on error."""
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="ignore"), resp.url
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="ignore"), url


def fetch_playwright(url, wait_ms=4000):
    """Returns (status, html, final_url) using headless Chromium."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=BROWSER_HEADERS["User-Agent"],
            locale="th-TH",
            extra_http_headers={"Accept-Language": "th,en-US;q=0.9,en;q=0.8"},
        )
        page = context.new_page()
        try:
            resp = page.goto(url, wait_until="networkidle", timeout=60000)
            status = resp.status if resp else 0
            page.wait_for_timeout(wait_ms)
            html = page.content()
            final_url = page.url
            # Print a visible-text snippet to help debug phrase matching
            snippet = re.sub(r"<[^>]+>", " ", html)
            snippet = re.sub(r"\s+", " ", snippet).strip()[:500]
            print(f"    Page snippet: {snippet}")
        except Exception as e:
            print(f"  Playwright error for {url}: {e}", file=sys.stderr)
            html = ""
            status = 0
            final_url = url
        finally:
            browser.close()
        return status, html, final_url

# ── Parsing ───────────────────────────────────────────────────────────────────

def is_page_gone(status, html, final_url, original_url, page_gone_phrases):
    """Return True if the product page no longer exists."""
    if status == 404:
        return True
    # Redirected to a much shorter path (i.e. homepage) → product gone
    orig_path = re.sub(r"https?://[^/]+", "", original_url).split("?")[0]
    final_path = re.sub(r"https?://[^/]+", "", final_url).split("?")[0]
    if len(final_path) < 5 and len(orig_path) > 15:
        return True
    html_lower = html.lower()
    for phrase in page_gone_phrases:
        if phrase.lower() in html_lower:
            return True
    return False


SONY_PRODUCT_LINK_RE = re.compile(
    r'href="(?:https://store\.sony\.co\.th)?(/collections/console/products/[^"?#]+)"',
    re.IGNORECASE,
)


def parse_sony_collection(html, out_of_stock_phrase):
    """Parse Sony store collection page → {product_url: {name, in_stock, exists}}."""
    seen = []
    for match in SONY_PRODUCT_LINK_RE.finditer(html):
        path = match.group(1)
        url = "https://store.sony.co.th" + path
        if url not in [u for u, _ in seen]:
            seen.append((url, path))

    if not seen:
        return {}

    positions = [html.find(p) for _, p in seen]
    products = {}
    for i, (url, _path) in enumerate(seen):
        start = positions[i]
        end = positions[i + 1] if i + 1 < len(seen) else len(html)
        chunk = html[start:end]
        heading = re.search(r"<h[1-4][^>]*>(.*?)</h[1-4]>", chunk, re.IGNORECASE | re.DOTALL)
        name = TAG_RE.sub("", heading.group(1)).strip() if heading else ""
        if not name:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            name = slug.replace("-", " ").title()
        products[url] = {
            "name": f"Sony Store: {name}",
            "in_stock": out_of_stock_phrase not in chunk,
            "exists": True,
        }
    return products


def check_site(site):
    """
    Fetch and evaluate one site config.
    Returns dict of {url: {name, in_stock, exists}} or None on network failure.
    """
    url = site["url"]
    name = site["name"]
    fetch_fn = (lambda u: fetch_playwright(u, wait_ms=8000)) if site.get("long_wait") else \
               (fetch_playwright if site["use_playwright"] else fetch_simple)

    print(f"  Checking {name}...")
    try:
        status, html, final_url = fetch_fn(url)
    except Exception as e:
        print(f"  NETWORK ERROR for {name}: {e}", file=sys.stderr)
        return None   # Skip this site this run; don't update state

    print(f"    HTTP {status}, {len(html)} bytes, final URL: {final_url[:80]}")

    if site["type"] == "collection":
        products = parse_sony_collection(html, site["out_of_stock_phrases"][0])
        if not products:
            print(f"  WARNING: no products parsed from {name}", file=sys.stderr)
            send_notification(
                f"⚠️ Stock checker warning",
                f"Couldn't parse any products from {name}. Layout may have changed - check Action logs.",
                url,
            )
            return None
        return products

    # Single product page
    gone = is_page_gone(status, html, final_url, url, site.get("page_gone_phrases", []))
    if gone:
        print(f"  PAGE GONE: {name}")
        return {url: {"name": name, "in_stock": False, "exists": False}}

    # Check stock status
    html_lower = html.lower()
    found_phrases = [p for p in site.get("out_of_stock_phrases", []) if p.lower() in html_lower]
    in_stock = len(found_phrases) == 0

    if len(html) < 2000:
        # Very short response often means a bot block / redirect we missed
        print(f"  WARNING: suspiciously short response for {name} ({len(html)} bytes)", file=sys.stderr)
        send_notification(
            f"⚠️ Stock checker warning",
            f"{name} returned only {len(html)} bytes - may be blocked. Check Action logs.",
            url,
        )
        return None

    print(f"    {'IN STOCK' if in_stock else 'out of stock'} (matched phrases: {found_phrases or 'none'})")
    return {url: {"name": name, "in_stock": in_stock, "exists": True}}

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            raw = json.load(f)
        # Migrate old format {url: bool} → new format {url: {in_stock, exists}}
        migrated = {}
        for k, v in raw.items():
            if isinstance(v, bool):
                migrated[k] = {"in_stock": v, "exists": True}
            else:
                migrated[k] = v
        return migrated
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Notifications ─────────────────────────────────────────────────────────────

def send_notification(title, message, url=None):
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "high",
    }
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  ntfy send failed: {e}", file=sys.stderr)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    old_state = load_state()
    is_first_run = len(old_state) == 0
    new_state = dict(old_state)  # start with old state; update as we go

    total_checked = 0
    notifications_sent = 0

    for site in SITES:
        result = check_site(site)
        if result is None:
            continue  # network/parse failure - leave old state unchanged

        for url, info in result.items():
            total_checked += 1
            prev = old_state.get(url)

            # Update state
            new_state[url] = {"in_stock": info["in_stock"], "exists": info["exists"]}

            # Page newly gone
            if not info["exists"]:
                if prev is None or prev.get("exists", True):
                    print(f"PAGE GONE: {info['name']}")
                    send_notification(
                        "⚠️ Product page missing!",
                        f"{info['name']} - the product page is no longer reachable. "
                        f"The URL may have changed. Tap to check.",
                        url,
                    )
                    notifications_sent += 1
                continue

            # Page came back after being gone
            if prev is not None and not prev.get("exists", True) and info["exists"]:
                print(f"PAGE RESTORED: {info['name']}")
                send_notification(
                    "ℹ️ Product page restored",
                    f"{info['name']} - the page is accessible again.",
                    url,
                )
                notifications_sent += 1

            if is_first_run or prev is None:
                # First time seeing this URL
                if not is_first_run:
                    status_str = "in stock" if info["in_stock"] else "currently out of stock"
                    print(f"NEW LISTING: {info['name']} ({status_str})")
                    send_notification(
                        "🆕 New listing appeared!",
                        f"{info['name']} - {status_str}",
                        url,
                    )
                    notifications_sent += 1
                continue

            prev_in_stock = prev.get("in_stock", False)
            if not prev_in_stock and info["in_stock"]:
                print(f"RESTOCK: {info['name']}")
                send_notification(
                    "✅ Back in stock!",
                    info["name"],
                    url,
                )
                notifications_sent += 1

    save_state(new_state)

    if notifications_sent == 0:
        print(f"\nNo changes detected. Checked {total_checked} product(s) across {len(SITES)} site(s).")
    else:
        print(f"\n{notifications_sent} notification(s) sent.")


if __name__ == "__main__":
    main()
