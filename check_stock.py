#!/usr/bin/env python3
"""
Checks the Sony Store Thailand consoles page for restocks and sends a push
notification via ntfy.sh when a product that was "Notify me when back in
stock" is no longer shown that way (i.e. it likely came back in stock).

State (which products were out of stock last run) is persisted to state.json
so this can run as a stateless cron job / GitHub Action.
"""

import json
import os
import re
import sys
import urllib.request

URL = "https://store.sony.co.th/collections/console"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "sony-stock-a8cebf9a48")
STATE_FILE = "state.json"

OUT_OF_STOCK_PHRASE = "Notify me when back in stock"

# Matches each product block: link text (product name) ... then later the
# out-of-stock phrase, scoped per product card.
PRODUCT_RE = re.compile(
    r'href="(https://store\.sony\.co\.th/collections/console/products/[^"]+)"[^>]*>\s*([^<]+?)\s*</a>',
    re.IGNORECASE,
)


def fetch_page():
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.status
        body = resp.read().decode("utf-8", errors="ignore")
        return status, body


# Finds any anchor whose href points to a product detail page.
PRODUCT_LINK_RE = re.compile(
    r'href="(https://store\.sony\.co\.th/collections/console/products/[^"?#]+)"',
    re.IGNORECASE,
)

# A reasonably permissive "visible text" grabber: strips tags from a chunk.
TAG_RE = re.compile(r"<[^>]+>")


def parse_products(html):
    """Return dict of {product_url: {"name": str, "in_stock": bool}}."""
    # Step 1: collect unique product URLs in order of first appearance.
    seen = []
    for match in PRODUCT_LINK_RE.finditer(html):
        url = match.group(1)
        if url not in seen:
            seen.append(url)

    if not seen:
        return {}

    positions = [html.find(u) for u in seen]

    products = {}
    for i, url in enumerate(seen):
        start = positions[i]
        end = positions[i + 1] if i + 1 < len(seen) else len(html)
        chunk = html[start:end]

        # Pull a product name out of the chunk: look for an <h*>-ish
        # heading first, else fall back to the slug from the URL.
        heading_match = re.search(r"<h[1-4][^>]*>(.*?)</h[1-4]>", chunk, re.IGNORECASE | re.DOTALL)
        if heading_match:
            name = TAG_RE.sub("", heading_match.group(1)).strip()
        else:
            name = ""
        if not name:
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            name = slug.replace("-", " ").title()

        in_stock = OUT_OF_STOCK_PHRASE not in chunk
        products[url] = {"name": name, "in_stock": in_stock}

    return products


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_notification(title, message, url=None):
    headers = {"Title": title.encode("utf-8")}
    if url:
        headers["Click"] = url
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    status, html = fetch_page()
    print(f"Fetched page: HTTP {status}, {len(html)} bytes")

    products = parse_products(html)

    if not products:
        snippet = re.sub(r"\s+", " ", html)[:400]
        print("WARNING: no products parsed - page structure may have changed.", file=sys.stderr)
        print(f"First 400 chars of response: {snippet}", file=sys.stderr)
        send_notification(
            "Stock checker warning",
            f"Couldn't parse any products (HTTP {status}, {len(html)} bytes). "
            "Site may be blocking automated requests or changed layout - check the Action logs.",
            URL,
        )
        sys.exit(1)

    old_state = load_state()
    is_first_run = len(old_state) == 0
    new_state = {}
    restocked = []
    new_products = []

    for url, info in products.items():
        new_state[url] = info["in_stock"]
        was_in_stock = old_state.get(url, "missing")  # "missing" if never seen before
        if was_in_stock == "missing":
            if not is_first_run:
                new_products.append((info["name"], url, info["in_stock"]))
        elif was_in_stock is False and info["in_stock"] is True:
            restocked.append((info["name"], url))

    save_state(new_state)

    for name, url, in_stock in new_products:
        status = "in stock" if in_stock else "currently out of stock"
        print(f"NEW PRODUCT: {name} ({status})")
        send_notification("New console listing!", f"{name} - {status}", url)

    for name, url in restocked:
        print(f"RESTOCK: {name}")
        send_notification("Back in stock!", name, url)

    if not restocked and not new_products:
        print(f"No changes. Checked {len(products)} product(s).")


if __name__ == "__main__":
    main()
