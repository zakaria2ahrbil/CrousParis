#!/usr/bin/env python3
"""
CROUS listing watcher — alerts you on Telegram the moment a new housing
listing appears anywhere in Île-de-France on trouverunlogement.lescrous.fr,
tagged with how convenient it is relative to Sorbonne Paris Nord (Villetaneuse).

This only READS the public search pages (no login, no auto-booking).
You still confirm/reserve manually once you get pinged — that's the safe part.

Config is read from environment variables (for GitHub Actions) with local
fallback defaults so you can also just run it on your own machine.
"""

import json
import os
import re
import time
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# ---------------- CONFIG ----------------

TOOL_ID = 45  # 2026-2027 campaign. Use 42 for the current year's tool.
BASE_URL = f"https://trouverunlogement.lescrous.fr/tools/{TOOL_ID}/search"

# All of Île-de-France, so nothing gets filtered out before you see it.
# IDF_POSTAL_PREFIXES = ["75", "77", "78", "91", "92", "93", "94", "95"]

# MAX_PRICE = 405  # euros/month

IDF_POSTAL_PREFIXES = ["0","1","2","3","4","5","6","7","8","9"]  # matches everything
MAX_PRICE = None  # no price cap

CHECK_INTERVAL_SECONDS = 300  # only used in local/loop mode, not GitHub Actions

STATE_FILE = Path(os.environ.get("STATE_FILE", "crous_seen_ids.json"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PUT_YOUR_CHAT_ID_HERE")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://trouverunlogement.lescrous.fr/",
    "Connection": "keep-alive",
}

DESTINATION = "Universit%C3%A9+Sorbonne+Paris+Nord,+Villetaneuse"

# ---------------- COMMUTE PREFERENCE TAGGING ----------------
# Rough, hand-estimated transit tiers relative to Villetaneuse (main campus)
# and Saint-Denis/Bobigny (other Sorbonne Paris Nord sites). These are
# estimates, not live routing — always check the Google Maps link included
# in the alert for the real transit time.

SPECIFIC_TIERS = {
    "93430": "🟢 Excellent (~10-15 min) — Villetaneuse, sur le campus",
    "93200": "🟢 Excellent (~15-20 min) — Saint-Denis, site Sorbonne Paris Nord",
    "93000": "🟢 Excellent (~20 min) — Bobigny, site Sorbonne Paris Nord",
    "93380": "🟢 Très bon (~20-25 min) — Pierrefitte-sur-Seine",
    "93800": "🟢 Très bon (~20-25 min) — Épinay-sur-Seine",
    "93210": "🟢 Très bon (~20-25 min) — Saint-Denis / La Plaine",
    "95120": "🟡 Correct (~30-40 min) — Ermont",
}

DEPT_TIERS = {
    "93": "🟡 Correct (~25-40 min selon secteur)",
    "75": "🟡 Correct (~30-50 min selon arrondissement)",
    "95": "🟠 Moyen (~40-70 min selon secteur)",
    "92": "🟠 Moyen (~45-60 min)",
    "94": "🟠 Moyen (~50-70 min)",
    "91": "🔴 Loin (~1h-1h30)",
    "77": "🔴 Très loin (~1h30 ou plus)",
    "78": "🔴 Très loin (~1h30 ou plus)",
}


def get_preference(postal: str) -> str:
    if postal in SPECIFIC_TIERS:
        return SPECIFIC_TIERS[postal]
    dept = postal[:2]
    return DEPT_TIERS.get(dept, "⚪ Distance inconnue — vérifie le lien Maps")


# ---------------- SCRAPING LOGIC ----------------

LISTING_LINK_RE = re.compile(rf"/tools/{TOOL_ID}/accommodations/(\d+)")
PRICE_RE = re.compile(r"([\d]+(?:,\d+)?)\s*€")
POSTAL_RE = re.compile(r"\b(\d{5})\b")


def fetch_page(page_num: int) -> BeautifulSoup:
    resp = requests.get(BASE_URL, params={"page": page_num}, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def get_last_page(soup: BeautifulSoup) -> int:
    max_page = 1
    for a in soup.find_all("a", href=re.compile(r"page=(\d+)")):
        m = re.search(r"page=(\d+)", a["href"])
        if m:
            max_page = max(max_page, int(m.group(1)))
    return max_page


def parse_listings(soup: BeautifulSoup):
    listings = []
    seen_on_page = set()
    for a in soup.find_all("a", href=LISTING_LINK_RE):
        m = LISTING_LINK_RE.search(a["href"])
        listing_id = m.group(1)
        if listing_id in seen_on_page:
            continue
        seen_on_page.add(listing_id)

        name = a.get_text(strip=True)

        card = a
        for _ in range(4):
            if card.parent is None:
                break
            card = card.parent
        card_text = card.get_text(" ", strip=True)

        price_match = PRICE_RE.search(card_text)
        price = price_match.group(1).replace(",", ".") if price_match else None

        postal_match = POSTAL_RE.search(card_text)
        postal = postal_match.group(1) if postal_match else None

        listings.append({
            "id": listing_id,
            "name": name,
            "url": f"https://trouverunlogement.lescrous.fr/tools/{TOOL_ID}/accommodations/{listing_id}",
            "price": float(price) if price else None,
            "postal": postal,
            "raw_text": card_text,
        })
    return listings


def get_all_listings():
    soup = fetch_page(1)
    last_page = get_last_page(soup)
    all_listings = parse_listings(soup)

    for page in range(2, last_page + 1):
        time.sleep(1)  # be polite between page requests
        soup = fetch_page(page)
        all_listings.extend(parse_listings(soup))

    return all_listings


def matches_criteria(listing) -> bool:
    if listing["postal"] is None:
        return False
    if not any(listing["postal"].startswith(p) for p in IDF_POSTAL_PREFIXES):
        return False
    if MAX_PRICE is not None and listing["price"] is not None and listing["price"] > MAX_PRICE:
        return False
    return True


def load_seen_ids() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen_ids(ids: set):
    STATE_FILE.write_text(json.dumps(sorted(ids)))


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[!] Telegram send failed: {e}", file=sys.stderr)


def build_message(listing) -> str:
    preference = get_preference(listing["postal"])
    maps_link = (
        f"https://www.google.com/maps/dir/?api=1&origin={quote(listing['postal'] + ', France')}"
        f"&destination={DESTINATION}&travelmode=transit"
    )
    return (
        f"🏠 Nouveau logement CROUS !\n"
        f"{listing['name']}\n"
        f"{listing['price']}€ — {listing['postal']}\n"
        f"Préférence : {preference}\n"
        f"Trajet réel : {maps_link}\n"
        f"Annonce : {listing['url']}"
    )


def run_once(seen_ids: set) -> set:
    listings = get_all_listings()
    matching = [l for l in listings if matches_criteria(l)]
    new_ones = [l for l in matching if l["id"] not in seen_ids]

    for listing in new_ones:
        msg = build_message(listing)
        print(msg)
        send_telegram(msg)

    return {l["id"] for l in matching}


def main():
    print(f"Watching IDF listings, max {MAX_PRICE}€...")
    seen_ids = load_seen_ids()
    # single-shot mode (used by GitHub Actions, which provides its own schedule)
    if os.environ.get("SINGLE_SHOT", "1") == "1":
        try:
            seen_ids = run_once(seen_ids)
            save_seen_ids(seen_ids)
        except Exception as e:
            print(f"[!] Error during check: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # continuous loop mode (used if you run it yourself on a machine)
    while True:
        try:
            seen_ids = run_once(seen_ids)
            save_seen_ids(seen_ids)
        except Exception as e:
            print(f"[!] Error during check: {e}", file=sys.stderr)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()