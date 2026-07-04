#!/usr/bin/env python3
"""
Ticket Price Monitor v2
Surveille les prix de billets et envoie un SMS via Twilio quand un prix
passe sous le seuil configuré.

- billets.ca : utilise l'API interne du site (vrais prix + quantités réelles).
  L'event_id est détecté automatiquement à partir de l'URL de la page.
- Autres sites : extraction générique des prix dans le HTML.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

# Cooldown entre deux alertes pour le même événement (en heures),
# sauf si le prix a encore baissé depuis la dernière alerte.
ALERT_COOLDOWN_HOURS = 6

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-CA,fr;q=0.9,en-CA;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

BILLETS_CA_API = "https://www.billets.ca/views/epic_seatmap/get_event_tickets.php?event_id={event_id}"

# Regex générique pour trouver des prix : 123 $, $123, 123,50 $, $1,234.56, etc.
PRICE_PATTERN = re.compile(
    r"(?:\$\s*(\d{1,4}(?:[ ,]\d{3})*(?:[.,]\d{2})?))"      # $123.45
    r"|(?:(\d{1,4}(?:[ ,]\d{3})*(?:[.,]\d{2})?)\s*\$)"     # 123,45 $
)

EVENT_ID_PATTERN = re.compile(r"event_id=(\d+)")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def parse_price(raw):
    """Convertit une string de prix ('1 234,56' ou '1,234.56') en float."""
    raw = str(raw).strip()
    raw = raw.replace(" ", "").replace("\u00a0", "")
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts[-1]) == 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_page(url, referer=None, retries=2):
    """Récupère une page/API avec retries."""
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["X-Requested-With"] = "XMLHttpRequest"
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.text
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code in (403, 429):
                break
        except requests.RequestException as e:
            last_err = str(e)
        if attempt < retries:
            time.sleep(5)
    raise RuntimeError(f"Échec du fetch : {last_err}")


# ---------------------------------------------------------------------------
# Mode billets.ca (API interne)
# ---------------------------------------------------------------------------

def collect_ticket_listings(node, out):
    """Parcourt récursivement le JSON de l'API billets.ca et ramasse tous les
    listings (dicts contenant sale_price + nb_tickets), peu importe la
    profondeur ou la structure exacte."""
    if isinstance(node, dict):
        if "sale_price" in node and "nb_tickets" in node:
            out.append(node)
        else:
            for value in node.values():
                collect_ticket_listings(value, out)
    elif isinstance(node, list):
        for item in node:
            collect_ticket_listings(item, out)


def listing_fits_quantity(listing, min_quantity):
    """Vérifie qu'un listing permet d'acheter au moins min_quantity billets
    ensemble. Le champ 'blocks' indique les regroupements vendables
    (ex. '2,4' = achetable en blocs de 2 ou de 4)."""
    try:
        nb = int(str(listing.get("nb_tickets", "0")))
    except ValueError:
        nb = 0
    if nb < min_quantity:
        return False
    blocks_raw = str(listing.get("blocks") or "").strip()
    if not blocks_raw:
        return True  # pas d'info de blocs → on se fie à nb_tickets
    blocks = []
    for b in blocks_raw.split(","):
        try:
            blocks.append(int(b.strip()))
        except ValueError:
            continue
    if not blocks:
        return True
    # OK si un bloc permet d'acheter au moins la quantité voulue
    return any(b >= min_quantity for b in blocks)


def check_billets_ca(event, event_url):
    """Retourne (min_price, quantity_dispo, nb_listings) pour un événement
    billets.ca, en passant par l'API interne du site."""
    min_quantity = int(event.get("min_quantity", 1))

    # 1. Trouver l'event_id dans le HTML de la page
    html = fetch_page(event_url)
    match = EVENT_ID_PATTERN.search(html)
    if not match:
        raise RuntimeError("event_id introuvable dans la page")
    event_id = match.group(1)

    # 2. Appeler l'API des billets
    api_url = BILLETS_CA_API.format(event_id=event_id)
    raw = fetch_page(api_url, referer=event_url)
    if not raw.strip():
        return None, None, 0  # aucun billet en vente

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("réponse API illisible (pas du JSON)")

    listings = []
    collect_ticket_listings(data, listings)

    eligible = []
    for listing in listings:
        if not listing_fits_quantity(listing, min_quantity):
            continue
        price = parse_price(listing.get("sale_price"))
        if price is not None and price > 0:
            try:
                nb = int(str(listing.get("nb_tickets", "0")))
            except ValueError:
                nb = 0
            eligible.append((price, nb))

    if not eligible:
        return None, None, len(listings)

    best_price, best_nb = min(eligible, key=lambda x: x[0])
    return best_price, best_nb, len(listings)


# ---------------------------------------------------------------------------
# Mode générique (autres sites)
# ---------------------------------------------------------------------------

def extract_prices_generic(html):
    """Extrait tous les montants en $ visibles dans la page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    prices = []
    for match in PRICE_PATTERN.finditer(text):
        raw = match.group(1) or match.group(2)
        price = parse_price(raw)
        if price is not None and 5 < price < 10000:
            prices.append(price)
    return prices


def extract_prices_selector(html, selector):
    """Extrait les prix via un sélecteur CSS spécifique."""
    soup = BeautifulSoup(html, "html.parser")
    prices = []
    for el in soup.select(selector):
        text = el.get_text(" ", strip=True)
        for match in PRICE_PATTERN.finditer(text):
            raw = match.group(1) or match.group(2)
            price = parse_price(raw)
            if price is not None and 5 < price < 10000:
                prices.append(price)
    return prices


def check_generic(event, event_url):
    """Retourne (min_price, None, nb_prix) via scraping HTML générique."""
    html = fetch_page(event_url)
    selector = event.get("css_selector")
    if selector:
        prices = extract_prices_selector(html, selector)
    else:
        prices = extract_prices_generic(html)
    if not prices:
        return None, None, 0
    return min(prices), None, len(prices)


# ---------------------------------------------------------------------------
# Alertes
# ---------------------------------------------------------------------------

def send_sms(body):
    """Envoie un SMS via l'API REST Twilio à un ou plusieurs numéros.

    ALERT_TO_NUMBER peut contenir plusieurs numéros séparés par des virgules :
    +1514XXXXXXX,+1450XXXXXXX
    """
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_num = os.environ.get("TWILIO_FROM_NUMBER")
    to_raw = os.environ.get("ALERT_TO_NUMBER", "")

    to_numbers = [n.strip() for n in to_raw.split(",") if n.strip()]

    if not all([sid, token, from_num]) or not to_numbers:
        log("⚠️  Secrets Twilio manquants — SMS non envoyé. Message :")
        log(body)
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    sent_any = False
    for to_num in to_numbers:
        resp = requests.post(
            url,
            auth=(sid, token),
            data={"From": from_num, "To": to_num, "Body": body},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            log(f"✅ SMS envoyé à {to_num}")
            sent_any = True
        else:
            log(f"❌ Erreur Twilio pour {to_num} ({resp.status_code}) : {resp.text[:200]}")
    return sent_any


def should_alert(event_state, min_price, now_ts):
    """Décide si on alerte : nouveau bas prix, ou cooldown expiré."""
    last_alert_ts = event_state.get("last_alert_ts", 0)
    last_alert_price = event_state.get("last_alert_price")

    cooldown_s = ALERT_COOLDOWN_HOURS * 3600
    cooldown_expired = (now_ts - last_alert_ts) > cooldown_s

    if last_alert_price is not None and min_price < last_alert_price * 0.95:
        return True
    return cooldown_expired


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    config = load_json(CONFIG_FILE, {"events": []})
    state = load_json(STATE_FILE, {})
    events = [e for e in config.get("events", []) if e.get("enabled", True)]

    if not events:
        log("Aucun événement actif dans config.json")
        return

    now_ts = int(time.time())
    alerts_sent = 0

    for event in events:
        name = event.get("name", "Sans nom")
        url = event.get("url")
        max_price = event.get("max_price")
        min_quantity = int(event.get("min_quantity", 1))

        if not url or max_price is None:
            log(f"⏭️  '{name}' : url ou max_price manquant, ignoré")
            continue

        log(f"🔍 Vérification : {name} (seuil : {max_price} $, min {min_quantity} billet(s))")
        event_state = state.setdefault(name, {})
        event_state["last_check_ts"] = now_ts

        is_billets_ca = "billets.ca" in url or "billet.ca" in url

        try:
            if is_billets_ca:
                min_price, qty, nb_listings = check_billets_ca(event, url)
            else:
                min_price, qty, nb_listings = check_generic(event, url)
        except RuntimeError as e:
            log(f"❌ {name} : {e}")
            event_state["last_error"] = str(e)
            continue

        event_state.pop("last_error", None)
        event_state["last_min_price"] = min_price

        if min_price is None:
            if nb_listings > 0:
                log(f"⚠️  {name} : {nb_listings} listing(s) trouvés mais aucun "
                    f"n'offre {min_quantity} billet(s) ensemble")
            else:
                log(f"⚠️  {name} : aucun billet en vente pour l'instant")
            continue

        qty_txt = f" (lot de {qty})" if qty else ""
        log(f"   Prix min : {min_price:.2f} ${qty_txt} — {nb_listings} listing(s) analysés")

        if min_price <= max_price:
            if should_alert(event_state, min_price, now_ts):
                # Message court : les comptes Twilio en mode essai ont une
                # limite de longueur (erreur 30044). Pas d'emoji ni d'URL.
                qty_sms = f" x{qty}" if qty else ""
                body = (
                    f"ALERTE BILLETS: {name[:40]} - "
                    f"{min_price:.2f}${qty_sms} (seuil {max_price}$)"
                )
                if send_sms(body):
                    alerts_sent += 1
                    event_state["last_alert_ts"] = now_ts
                    event_state["last_alert_price"] = min_price
            else:
                log(f"   Sous le seuil, mais alerte récente → cooldown actif")
        else:
            log(f"   Au-dessus du seuil, pas d'alerte")

    save_json(STATE_FILE, state)
    log(f"Terminé. {alerts_sent} alerte(s) envoyée(s).")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"💥 Erreur fatale : {e}")
        sys.exit(1)
