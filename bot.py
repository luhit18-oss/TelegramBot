from flask import Flask, request, jsonify
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple
import requests

# ============== EDITA SOLO ESTAS 3 VARIABLES ==============
TOKEN = "8280812701:AAGH4X-HoahE_jA6foiV0oo61CQrMuLd9hM"
BASE_URL = "https://puremusebot.onrender.com"
MP_ACCESS_TOKEN = "APP_USR-3510033415376991-101723-4123f543520272287c00983a3ca15c83-95374565"
# =========================================================

SEND_URL     = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL   = "https://api.mercadopago.com/v1/payments/"

app = Flask(__name__)

# -------------------- Utilidades --------------------
def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def tg_send(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(SEND_URL, json=payload, timeout=15)
    except Exception as e:
        app.logger.error(f"tg_send error: {e}")

# -------------------- galleries.txt en memoria --------------------
GALLERIES: List[Tuple[str, str]] = []  # [(title, url), ...] (title puede ser "")

def load_galleries(file_path: str = "galleries.txt") -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if not os.path.exists(file_path):
        app.logger.warning("galleries.txt no encontrado; continuando sin galerías.")
        return out
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) == 2:
                    title, url = parts[0].strip(), parts[1].strip()
                else:
                    title, url = "", parts[0].strip()
                if url.startswith("http"):
                    out.append((title, url))
        app.logger.info(f"Cargadas {len(out)} galerías desde {file_path}")
    except Exception as e:
        app.logger.error(f"load_galleries error: {e}")
    return out

GALLERIES = load_galleries()

# Progreso en memoria: chat_id -> índice (posición en GALLERIES)
USER_PROGRESS: Dict[int, int] = {}

def next_gallery_for(chat_id: int) -> Optional[Tuple[str, str]]:
    """Devuelve la siguiente galería para este usuario (sin repetir)."""
    if not GALLERIES:
        return None
    idx = USER_PROGRESS.get(chat_id, -1) + 1
    if idx >= len(GALLERIES):
        return None  # ya recibió todas las disponibles
    USER_PROGRESS[chat_id] = idx
    return GALLERIES[idx]

# -------------------- Mercado Pago --------------------
def mp_create_preference_for_user(chat_id: int, title="PureMuse VIP – 30 days", qty=1, unit_price=99.0, currency_id="MXN") -> str:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    body = {
        "items": [{
            "title": title,
            "quantity": int(qty),
            "unit_price": float(unit_price),
            "currency_id": currency_id
        }],
        "auto_return": "approved",
        "back_urls": {
            "success": f"{BASE_URL}/mp/return?status=success",
            "failure": f"{BASE_URL}/mp/return?status=failure",
            "pending": f"{BASE_URL}/mp/return?status=pending",
        },
        "notification_url": f"{BASE_URL}/mp/webhook",
        "external_reference": str(chat_id)
    }
    r = requests.post(MP_PREFS_URL, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("init_point") or data.get("sandbox_init_point")

def mp_get_payment(payment_id: str):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(MP_PAY_URL + str(payment_id), headers=headers, timeout=30)
    return r.json() if r.status_code == 200 else None

# -------------------- Textos --------------------
WELCOME = "🌹 *Welcome to PureMuse.*\nWhere art meets sensuality.\n\nChoose one of the options below 👇"
ABOUT   = "*PureMuse* is a digital gallery where art and sensuality merge.\nExclusive photographic collections, elegant aesthetics, and the beauty of desire."
COLLECT = "🖼️ *PureMuse Collections*\n• Noir & Gold Edition\n• Veils & Silhouettes\n• Amber Light\n_(Demo)_"
HELP    = "Available commands:\n/start, /about, /collections, /pay, /content, /support, /help"
PAID_OK = "✅ Payment received. Thanks! (Stage 2)\n*Note:* Access control activates in the next stage."

# -------------------- Rutas --------------------
@app.route("/", methods=["GET"])
def home():
    return "PureMuse Bot is up ✨ Stage 2 (no DB)", 200

@app.route("/webhook", methods=["POST","GET"])
def webhook():
    if request.method == "GET":
        return "Webhook OK", 200

    data = request.get_json(silent=True) or {}
    msg  = data.get("message") or data.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip().lower()

    if not chat_id or not text:
        return "OK", 200

    if text.startswith("/start"):
        keyboard = {
            "keyboard": [
                [{"text": "/about"}, {"text": "/collections"}],
                [{"text": "/pay"}, {"text": "/content"}],
                [{"text": "/support"}]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False
        }
        tg_send(chat_id, WELCOME, reply_markup=keyboard)

    elif text.startswith("/about"):
        tg_send(chat_id, ABOUT)

    elif text.startswith("/collections"):
        tg_send(chat_id, COLLECT)

    elif text.startswith("/help"):
        tg_send(chat_id, HELP)

    elif text.startswith("/content"):
        gal = next_gallery_for(chat_id)
        if not gal:
            if not GALLERIES:
                tg_send(chat_id, "ℹ️ There are no galleries loaded yet. Please try again later.")
            else:
                tg_send(chat_id, "🎉 You already received all available galleries. Come back later!")
        else:
            title, url = gal
            title_txt = f"*{title}*\n" if title else ""
            tg_send(chat_id, f"🍷 *Premium Gallery (Stage 2)*\n{title_txt}{url}")

    elif text.startswith("/pay") or text.startswith("/buy"):
        try:
            pay_url = mp_create_preference_for_user(chat_id)
            tg_send(chat_id, f"💎 *VIP Access (30 days)*\nPrice: $99 MXN\n👉 [Pay now]({pay_url})")
        except Exception as e:
            tg_send(chat_id, f"⚠️ Error creating payment link: {e}")

    elif text.startswith("/support"):
        tg_send(chat_id, "✉️ Support: contact@puremuse.example\nReplies within 24–48 hours.")

    else:
        tg_send(chat_id, "Unknown command. Use /help to see options.")
    return "OK", 200

@app.route("/mp/webhook", methods=["POST","GET"])
def mp_webhook():
    if request.method == "GET":
        return "MP Webhook OK", 200

    body = request.get_json(silent=True) or {}
    topic = body.get("type") or body.get("topic")
    payment_id = (body.get("data") or {}).get("id")

    if topic == "payment" and payment_id:
        pay = mp_get_payment(payment_id)
        if pay:
            status  = pay.get("status")
            ext_ref = pay.get("external_reference")
            app.logger.info(f"[MP] Payment {payment_id} -> {status} (ext_ref={ext_ref})")
            # Stage 2: no DB activation yet
    return jsonify({"status": "received"}), 200
