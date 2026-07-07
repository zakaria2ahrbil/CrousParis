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

TOOL_ID = 47  # confirmed from the live site's "année prochaine 2026-2027" link
BASE_URL = f"https://trouverunlogement.lescrous.fr/tools/{TOOL_ID}/search"

# All of Île-de-France, so nothing gets filtered out before you see it.
IDF_POSTAL_PREFIXES = ["75", "77", "78", "91", "92", "93", "94", "95"]

MAX_PRICE = None  # no price cap — show everything in IDF, you decide

CHECK_INTERVAL_SECONDS = 300  # only used in local/loop mode, not GitHub Actions

STATE_FILE = Path(os.environ.get("STATE_FILE", "crous_seen_ids.json"))
HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "crous_last_heartbeat.txt"))
HEARTBEAT_INTERVAL_SECONDS = 3600  # send an "I'm alive" ping at most once per hour

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
POSTAL_RE = re.compile(r"(\d{5})\s+[A-ZÀ-ÜŒ]")


WAIT_SCREEN_TEXT = "trop nombreux"


class SiteOverloadedError(Exception):
    """Raised when the site shows its 'trop nombreux' overload screen instead of real listings."""
    pass


def fetch_page(page_num: int, retries: int = 3) -> BeautifulSoup:
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(BASE_URL, params={"page": page_num}, headers=HEADERS, timeout=40)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")
            if WAIT_SCREEN_TEXT in soup.get_text().lower():
                raise SiteOverloadedError("Site returned the 'trop nombreux' overload screen")
            return soup
        except requests.exceptions.RequestException as e:
            last_exc = e
            print(f"[debug] fetch attempt {attempt + 1}/{retries} failed: {e}")
            time.sleep(5)
    raise last_exc


def get_last_page(soup: BeautifulSoup) -> int:
    # The site shows a "Dernière page" link with an href containing page=N
    max_page = 1
    for a in soup.find_all("a", class_="fr-pagination__link--last", href=True):
        m = re.search(r"page=(\d+)", a["href"])
        if m:
            max_page = max(max_page, int(m.group(1)))
    return max_page


def parse_price_range(price_text: str):
    """Handles both 'X €' and 'de X à Y €' formats. Returns (min_price, max_price)."""
    numbers = re.findall(r"[\d]+(?:,\d+)?", price_text)
    numbers = [float(n.replace(",", ".")) for n in numbers]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def parse_listings(soup: BeautifulSoup):
    listings = []
    for card in soup.select("div.fr-card"):
        title_link = card.select_one("h3.fr-card__title a[href]")
        if not title_link:
            continue

        m = LISTING_LINK_RE.search(title_link["href"])
        if not m:
            continue
        listing_id = m.group(1)
        name = title_link.get_text(strip=True)

        desc_el = card.select_one("p.fr-card__desc")
        address_text = desc_el.get_text(strip=True) if desc_el else ""
        postal_match = POSTAL_RE.search(address_text)
        postal = postal_match.group(1) if postal_match else None

        price_el = card.select_one("ul.fr-badges-group p.fr-badge")
        price_text = price_el.get_text(strip=True) if price_el else ""
        min_price, max_price = parse_price_range(price_text)

        listings.append({
            "id": listing_id,
            "name": name,
            "url": f"https://trouverunlogement.lescrous.fr/tools/{TOOL_ID}/accommodations/{listing_id}",
            "min_price": min_price,
            "max_price": max_price,
            "price_text": price_text,
            "address": address_text,
            "postal": postal,
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
    # Use min_price: if ANY room in this residence could be under budget, surface it.
    # The alert shows the full range so you can judge for yourself.
    if MAX_PRICE is not None and listing["min_price"] is not None and listing["min_price"] > MAX_PRICE:
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
        f"{listing['address']}\n"
        f"Prix : {listing['price_text']}\n"
        f"Préférence : {preference}\n"
        f"Trajet réel : {maps_link}\n"
        f"Annonce : {listing['url']}"
    )


def maybe_send_heartbeat(all_listings: list, matching: list):
    """Send a 'still watching' ping at most once per HEARTBEAT_INTERVAL_SECONDS,
    listing current IDF listings first, then a full-France list as a backup view."""
    now = time.time()
    last = 0.0
    if HEARTBEAT_FILE.exists():
        try:
            last = float(HEARTBEAT_FILE.read_text().strip())
        except ValueError:
            last = 0.0

    if now - last < HEARTBEAT_INTERVAL_SECONDS:
        return

    lines = ["✅ Bot actif\n"]

    if not matching:
        lines.append("Île-de-France : aucun logement pour l'instant.\n")
    else:
        lines.append(f"Île-de-France ({len(matching)}) :")
        for l in matching:
            pref = get_preference(l["postal"])
            lines.append(f"• {l['name']} — {l['price_text']} — {l['postal']} ({pref})\n  {l['url']}")
        lines.append("")

    if not all_listings:
        lines.append("France entière : aucun logement pour l'instant.")
    else:
        lines.append(f"France entière ({len(all_listings)}) — liste complète en backup :")
        for l in all_listings:
            lines.append(f"• {l['name']} — {l['price_text']} — {l['postal']} ({l['address']})\n  {l['url']}")

    # Telegram messages have a ~4096 char limit — split into chunks if needed
    full_text = "\n".join(lines)
    chunk_size = 3800
    for i in range(0, len(full_text), chunk_size):
        send_telegram(full_text[i:i + chunk_size])

    HEARTBEAT_FILE.write_text(str(now))


def run_once(seen_ids: set) -> set:
    try:
        listings = get_all_listings()
    except SiteOverloadedError:
        print("[debug] Site is overloaded ('trop nombreux' screen) — skipping this check, "
              "state unchanged. This is NOT a real 0 results.")
        return seen_ids
    except requests.exceptions.RequestException as e:
        print(f"[debug] Network error after retries ({e}) — skipping this check, state unchanged.")
        return seen_ids

    print(f"[debug] total listings scraped: {len(listings)}")
    if listings:
        print(f"[debug] sample listing: {listings[0]}")
    matching = [l for l in listings if matches_criteria(l)]
    print(f"[debug] listings matching filter: {len(matching)}")
    new_ones = [l for l in matching if l["id"] not in seen_ids]
    print(f"[debug] new (unseen) matching listings: {len(new_ones)}")

    for listing in new_ones:
        msg = build_message(listing)
        print(msg)
        send_telegram(msg)

    if not new_ones:
        maybe_send_heartbeat(listings, matching)

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
