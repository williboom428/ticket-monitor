#!/usr/bin/env python3
"""
Ticket Price Monitor
Surveille les prix de billets (billet.ca, Ticketmaster, etc.)
et envoie un SMS via Twilio quand un prix passe sous le seuil configuré.
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

# Regex générique pour trouver des prix : 123 $, $123, 123,50 $, $1,234.56, etc.
PRICE_PATTERN = re.compile(
    r"(?:\$\s*(\d{1,4}(?:[ ,]\d{3})*(?:[.,]\d{2})?))"      # $123.45
    r"|(?:(\d{1,4}(?:[ ,]\d{3})*(?:[.,]\d{2})?)\s*\$)"     # 123,45 $
)


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
    raw = raw.strip()
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


def fetch_page(url, retries=2):
    """Récupère la page avec retries."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
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
        selector = event.get("css_selector")

        if not url or max_price is None:
            log(f"⏭️  '{name}' : url ou max_price manquant, ignoré")
            continue

        log(f"🔍 Vérification : {name} (seuil : {max_price} $)")
        event_state = state.setdefault(name, {})

        try:
            html = fetch_page(url)
        except RuntimeError as e:
            log(f"❌ {name} : {e}")
            event_state["last_error"] = str(e)
            event_state["last_check_ts"] = now_ts
            continue

        if selector:
            prices = extract_prices_selector(html, selector)
        else:
            prices = extract_prices_generic(html)

        event_state["last_check_ts"] = now_ts
        event_state.pop("last_error", None)

        if not prices:
            log(f"⚠️  {name} : aucun prix détecté sur la page")
            event_state["last_min_price"] = None
            continue

        min_price = min(prices)
        event_state["last_min_price"] = min_price
        log(f"   Prix min trouvé : {min_price:.2f} $ ({len(prices)} prix détectés)")

        if min_price <= max_price:
            if should_alert(event_state, min_price, now_ts):
                body = (
                    f"🎟️ ALERTE BILLETS\n"
                    f"{name}\n"
                    f"Prix trouvé : {min_price:.2f} $ (seuil : {max_price} $)\n"
                    f"{url}"
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
