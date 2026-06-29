#!/usr/bin/env python3
"""
Tracks price and stock of SEVERAL Cyberpuerta products and notifies via
Telegram when:
  - the price reaches a product's desired_price or drops below it
    (alerts only when the price CHANGES while at/under the target)
  - fewer pieces remain than a product's min_stock (alerts once per crossing)

Products are configured in products.json (one entry each). To add more, just
append another entry to that file; no need to touch this script.

Uses cloudscraper to get past Cloudflare's challenge.

Local usage:
    pip install cloudscraper beautifulsoup4
    export TELEGRAM_TOKEN="123:ABC..."
    export TELEGRAM_CHAT_ID="987654321"
    python cyberpuerta_tracker.py
"""

import json
import os
import re
import sys
from pathlib import Path

import cloudscraper
from bs4 import BeautifulSoup

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

BASE = Path(__file__).parent
PRODUCTS = BASE / "products.json"
STATE = BASE / "state.json"


# ---------------------------------------------------------------------------
# Extraction (price + stock)
# ---------------------------------------------------------------------------
def to_number(text):
    """'$92,079.00' -> 92079.0"""
    if text is None:
        return None
    clean = re.sub(r"[^\d.,]", "", str(text))
    if "," in clean and "." in clean:
        clean = clean.replace(",", "")
    elif "," in clean:
        clean = clean.replace(",", "" if len(clean.split(",")[-1]) == 3 else ".")
    try:
        return float(clean)
    except ValueError:
        return None


def get_data(url, scraper):
    """Returns (price, stock). Either may be None if not detected.
    stock = 0 if the product is marked as out of stock."""
    resp = scraper.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Price: the <h2> inside the price box holds the CURRENT price.
    # (On discounted items the box also has a struck-through "price-from"
    # <span>; selecting the whole box would merge both numbers and fail.)
    price = None
    el = soup.select_one(".pdp-price-info__price h2")
    if el:
        price = to_number(el.get_text())

    # Stock: "234 pzas. disponibles" / "Solo 7 pzas. disponibles"
    text = soup.get_text(" ", strip=True)
    stock = None
    m = re.search(r"([\d,]+)\s*pzas?\.?\s*disponible", text, re.IGNORECASE)
    if m:
        stock = int(m.group(1).replace(",", ""))
    elif re.search(r"no cuenta con existencias|sin existencias|agotado", text, re.IGNORECASE):
        stock = 0

    return price, stock


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def notify(message, scraper):
    print(message)
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("(Telegram not configured; printing only)")
        return
    scraper.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=20,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def check(product, price, stock, st, scraper):
    """Applies the alert logic for one product and updates its state 'st'."""
    name = product.get("name", product["url"])
    desired = float(product.get("desired_price", 0) or 0)
    minimum = int(product.get("min_stock", 0) or 0)
    url = product["url"]

    # ----- PRICE alert: fires only when the price CHANGES while at/under target -----
    # Alerts the first time it drops to/under target, then again only if the
    # price is different from the last one we alerted at. Quiet while unchanged.
    if price is not None and desired and price <= desired:
        last_alerted_price = st.get("last_alerted_price")
        if last_alerted_price is None or price != last_alerted_price:
            notify(
                f"🎯 <b>{name}</b>\n"
                f"Target price reached!\n"
                f"Current: <b>${price:,.2f}</b> (target: ${desired:,.2f})\n"
                f"{url}",
                scraper,
            )
            st["last_alerted_price"] = price
    else:
        st["last_alerted_price"] = None  # back above target: re-arm

    # ----- LOW STOCK alert: fires only on each NEW lower level -----
    # Once below 'minimum', alert when stock drops to a value lower than the
    # last one we alerted at. Stays quiet while the number is unchanged.
    if stock is not None and minimum and stock < minimum:
        last_alerted = st.get("last_alerted_stock")
        if last_alerted is None or stock < last_alerted:
            if stock == 0:
                notify(f"⚠️ <b>{name}</b>\nOUT OF STOCK\n{url}", scraper)
            else:
                notify(
                    f"⏳ <b>{name}</b>\n"
                    f"Low stock!\n"
                    f"Available: <b>{stock}</b> (min: {minimum})\n"
                    f"{url}",
                    scraper,
                )
            st["last_alerted_stock"] = stock
    else:
        st["last_alerted_stock"] = None  # restocked at/above min: re-arm

    st["last_price"] = price
    return st


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    products = read_json(PRODUCTS, [])
    if not products:
        sys.exit("products.json is empty or missing.")

    state = read_json(STATE, {})
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    for product in products:
        url = product.get("url", "")
        name = product.get("name", url)
        if not url.startswith("http"):
            print(f"[SKIP] {name}: URL not configured yet.")
            continue

        try:
            price, stock = get_data(url, scraper)
        except Exception as ex:               # one failing product won't stop the rest
            print(f"[ERROR] {name}: {ex}")
            continue

        print(f"[OK] {name} -> price={price}  stock={stock}")
        st = state.get(url, {"last_price": None, "last_alerted_price": None, "last_alerted_stock": None})
        state[url] = check(product, price, stock, st, scraper)

    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()