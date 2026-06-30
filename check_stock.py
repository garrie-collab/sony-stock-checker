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
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (stock-checker-bot)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_products(html):
    """Return dict of {product_url: {"name": str, "in_stock": bool}}."""
    products = {}
    # Find all product detail links with their visible name
    for match in PRODUCT_RE.finditer(html):
        url, name = match.group(1), match.group(2).strip()
        if url not in products and name and "View Detail" not in name:
            products[url] = {"name": name, "in_stock": True}

    # For each product url, look at the slice of HTML between this product's
    # link and the next one, and check whether the out-of-stock phrase
    # appears in that slice.
    urls = list(products.keys())
    positions = [html.find(u) for u in urls]
    for i, url in enumerate(urls):
        start = positions[i]
        end = positions[i + 1] if i + 1 < len(urls) else len(html)
        chunk = html[start:end]
        products[url]["in_stock"] = OUT_OF_STOCK_PHRASE not in chunk

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
    html = fetch_page()
    products = parse_products(html)

    if not products:
        print("WARNING: no products parsed - page structure may have changed.", file=sys.stderr)
        send_notification(
            "Stock checker warning",
            "Couldn't parse any products from the Sony page. The site layout may have changed.",
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
