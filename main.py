import time
import random
import os
import logging
import requests
import json
from urllib.parse import urlparse, parse_qs

# ----------------------
# 1. CONFIGURATION
# ----------------------
VINTED_URLS = os.getenv("VINTED_URLS", "").split(',')
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
SEEN_FILE = "seen.json"

# Durée du run en secondes. Le repo est public -> minutes Actions illimitées,
# mais un job hébergé GitHub est de toute façon plafonné à 6h : on reste
# volontairement en dessous pour laisser de la marge au relais du cron.
RUN_DURATION = 5 * 3600 + 50 * 60  # 5h50

# Combien de temps on garde une annonce dans la mémoire "seen" avant de
# l'oublier (évite que seen.json grossisse indéfiniment).
SEEN_TTL_DAYS = 30

# Configuration pour les alertes d'erreur (si DISCORD_ERROR_WEBHOOK est défini)
ERROR_WEBHOOK = os.getenv("DISCORD_ERROR_WEBHOOK", DISCORD_WEBHOOK)  # Utilise le webhook principal par défaut
ERROR_COLOR = 15158332  # Rouge pour l'alerte

if not VINTED_URLS:
    raise SystemExit("⚠️ VINTED_URLS non configuré dans les Secrets.")
if not DISCORD_WEBHOOK:
    raise SystemExit("⚠️ DISCORD_WEBHOOK non configuré dans les Secrets.")

# ----------------------
# 2. LOGGING
# ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("goupil")

# ----------------------
# 3. SESSION HTTP
# ----------------------
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://www.vinted.fr/",
    "Connection": "keep-alive",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
})

# ----------------------
# 4. MEMOIRE PERSISTANTE (avec purge par ancienneté)
# ----------------------
def load_seen():
    """seen.json est maintenant un dict {lien: timestamp_premiere_vue}.
    Compatible avec l'ancien format (liste de liens) : les anciens liens
    sont réimportés avec le timestamp actuel."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            now = time.time()
            return {link: now for link in data}
        return data
    return {}

def save_seen(seen_items):
    # Purge des entrées plus vieilles que SEEN_TTL_DAYS
    cutoff = time.time() - SEEN_TTL_DAYS * 86400
    pruned = {link: ts for link, ts in seen_items.items() if ts >= cutoff}
    removed = len(seen_items) - len(pruned)
    if removed:
        logger.info(f"🧹 {removed} ancienne(s) entrée(s) purgée(s) de seen.json")
    with open(SEEN_FILE, "w") as f:
        json.dump(pruned, f)
    return pruned

seen_items = load_seen()

# ----------------------
# 4bis. API VINTED (au lieu du HTML, qui est maintenant chargé en JS)
# ----------------------
API_URL = "https://www.vinted.fr/api/v2/catalog/items"
_last_warmup = 0
WARMUP_INTERVAL = 15 * 60  # ré-établit le cookie anti-bot toutes les 15 min

def warm_up():
    """Visite la page d'accueil pour obtenir les cookies anti-bot (Datadome)
    nécessaires avant d'appeler l'API. À refaire régulièrement, le cookie expire."""
    global _last_warmup
    try:
        session.get("https://www.vinted.fr/", timeout=12)
        _last_warmup = time.time()
        logger.info("🍪 Cookies de session renouvelés")
    except Exception as e:
        logger.warning(f"Échec du renouvellement des cookies : {e}")

def maybe_warm_up():
    if time.time() - _last_warmup > WARMUP_INTERVAL:
        warm_up()

def build_api_params(vinted_url):
    """Convertit une URL de recherche Vinted (copiée depuis le site)
    en paramètres pour l'API interne /api/v2/catalog/items."""
    qs = parse_qs(urlparse(vinted_url).query)
    params = {
        "search_text": qs.get("search_text", [""])[0],
        "order": qs.get("order", ["newest_first"])[0],
        "page": 1,
        "per_page": 20,
    }
    catalog_ids = qs.get("catalog[]") or qs.get("catalog_ids")
    if catalog_ids:
        params["catalog_ids"] = ",".join(catalog_ids)
    return params

def parse_item(item):
    """Extrait titre / prix / lien / photo d'un item JSON de l'API, en gérant
    plusieurs formats possibles (l'API évolue de temps en temps elle aussi)."""
    title = item.get("title", "Article sans titre")

    price_obj = item.get("total_item_price") or item.get("price")
    if isinstance(price_obj, dict):
        amount = price_obj.get("amount", "?")
        currency = price_obj.get("currency_code", "EUR")
        price = f"{amount} {currency}"
    else:
        price = str(price_obj) if price_obj else "prix inconnu"

    link = item.get("url")
    if not link and item.get("id"):
        link = f"https://www.vinted.fr/items/{item['id']}"

    photo = item.get("photo") or {}
    if not photo and item.get("photos"):
        photo = item["photos"][0] if item["photos"] else {}
    img_url = photo.get("url", "") if isinstance(photo, dict) else ""

    return title, price, link, img_url

# ----------------------
# 5. DISCORD
# ----------------------
def send_status_message(message_content):
    # Utilise un webhook dédié au statut si défini, sinon retombe sur le webhook principal
    status_webhook_url = os.getenv("DISCORD_WEBHOOK_STATUS", DISCORD_WEBHOOK)
    if not status_webhook_url:
        logger.warning("Aucun webhook de statut configuré, message ignoré.")
        return
    message = {"content": message_content}
    try:
        requests.post(status_webhook_url, json=message, timeout=10)
        logger.info("Message de statut envoyé avec succès.")
    except Exception as e:
        logger.error(f"Erreur lors de l'envoi du message de statut : {e}")

def send_to_discord(title, price, link, img_url=""):
    if not title or not link:
        logger.warning("Titre ou lien vide, notification Discord ignorée")
        return
    data = {
        "embeds": [{
            "title": f"{title} - {price}",
            "url": link,
            "color": 3447003,
            "image": {"url": img_url} if img_url else None
        }]
    }
    try:
        resp = session.post(DISCORD_WEBHOOK, json=data, timeout=10)
        if resp.status_code // 100 != 2:
            logger.warning(f"Discord Webhook renvoyé {resp.status_code}")
    except Exception as e:
        logger.error(f"Erreur en envoyant à Discord : {e}")

def send_error_alert(error_type, details, url="N/A"):
    details_str = str(details)[:1500]
    data = {
        "embeds": [{
            "title": f"❌ ALERTE ERREUR SCRAPING : {error_type}",
            "description": f"**URL** : `{url}`\n**Détails** : ```{details_str}```",
            "color": ERROR_COLOR,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        }]
    }
    try:
        resp = requests.post(ERROR_WEBHOOK, json=data, timeout=10)
        if resp.status_code // 100 != 2:
            logger.error(f"Webhook d'alerte Discord renvoyé {resp.status_code}")
    except Exception as e:
        logger.error(f"Erreur CRITIQUE lors de l'envoi de l'alerte d'erreur : {e}")

# ----------------------
# 6. SCRAPER VINTED (one-shot)
# ----------------------
def check_vinted():
    global seen_items
    total_new_items = 0
    now = time.time()

    maybe_warm_up()

    for url in VINTED_URLS:
        logger.info(f"🌐 Analyse de l'URL : {url}")
        try:
            params = build_api_params(url)
            resp = session.get(
                API_URL,
                params=params,
                headers={"Accept": "application/json, text/plain, */*"},
                timeout=12,
            )

            if resp.status_code == 403:
                logger.error(f"🔴 ERREUR CRITIQUE 403 pour l'URL {url}. Blocage anti-bot (Datadome).")
                send_error_alert("HTTP 403 FORBIDDEN - BLOCAGE",
                                  "Le cookie anti-bot est probablement expiré ou invalide, ou l'IP est bloquée.",
                                  url)
                # on force un renouvellement de cookies au prochain tour
                global _last_warmup
                _last_warmup = 0
                continue

            if resp.status_code != 200:
                logger.warning(f"Réponse inattendue {resp.status_code} pour l'URL {url}")
                send_error_alert(f"HTTP {resp.status_code} Bloqué", f"Statut : {resp.status_code}", url)
                continue

            try:
                data = resp.json()
            except ValueError as e:
                logger.warning(f"❌ Réponse non-JSON pour l'URL {url}")
                send_error_alert("Réponse API illisible", f"La réponse n'est pas du JSON valide : {e}", url)
                continue

            items = data.get("items", [])
            if not items and "items" not in data:
                logger.warning(f"❌ Clé 'items' absente de la réponse pour l'URL {url}")
                send_error_alert("Structure API changée", f"Clés reçues : {list(data.keys())}", url)
                continue

            new_items_count = 0

            for item in items[:20]:
                try:
                    title, price, link, img_url = parse_item(item)
                    if not link:
                        continue

                    if link in seen_items:
                        continue

                    seen_items[link] = now
                    new_items_count += 1

                    logger.info(f"🔔 Nouvelle annonce : {title} - {price}\n🔗 {link}")
                    send_to_discord(title, price, link, img_url)
                    time.sleep(1.5)

                except Exception as e:
                    logger.error(f"Erreur traitement annonce pour l'URL {url}: {e}")
                    send_error_alert("Erreur Traitement Annonce", e, url)

            total_new_items += new_items_count

        except requests.exceptions.Timeout as e:
            logger.error(f"Erreur de Timeout pour l'URL {url}: {e}")
            send_error_alert("Erreur Timeout", e, url)
        except requests.exceptions.RequestException as e:
            logger.error(f"Erreur requête pour l'URL {url}: {e}")
            send_error_alert("Erreur Réseau/Requête", e, url)
        except Exception as e:
            logger.error(f"Erreur scraping pour l'URL {url}: {e}")
            send_error_alert("Erreur Inconnue", e, url)

    seen_items = save_seen(seen_items)
    logger.info("💾 Fichier seen.json mis à jour après ce scraping")

    if total_new_items == 0:
        logger.info("✅ Aucune nouvelle annonce sur toutes les URL")
    else:
        logger.info(f"🔔 {total_new_items} nouvelles annonces envoyées au total")

# ----------------------
# 7. BOUCLE BOT AVEC DUREE LIMITEE
# ----------------------
def bot_loop():
    warm_up()
    end_time = time.time() + RUN_DURATION
    while time.time() < end_time:
        logger.info("▶️ Nouvelle analyse...")
        check_vinted()

        # Pause aléatoire entre 2 et 5 minutes, sans dépasser la fin du run
        delay = random.uniform(120, 300)
        time_remaining = end_time - time.time()
        if time_remaining <= 0:
            break
        sleep_time = min(delay, time_remaining)
        logger.info(f"🔍 Prochaine analyse dans {int(sleep_time)} secondes")
        time.sleep(sleep_time)

    logger.info("🏁 Fin du run")
    save_seen(seen_items)
    send_status_message("✅ Run terminé !")

# ----------------------
# 8. LANCEMENT
# ----------------------
if __name__ == "__main__":
    logger.info("🚀 Bot Vinted démarré (one-shot)")
    logger.info(f"📡 URL Vinted : {VINTED_URLS}")
    send_status_message("🚀 C'est parti mon kiki !")
    bot_loop()
