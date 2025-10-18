from flask import Flask, request, jsonify
import os
import requests

# ========= EDITA SOLO ESTAS 3 VARIABLES =========
TOKEN = "YOUR_TELEGRAM_TOKEN"                      # ej: 12345:ABCDEF...
BASE_URL = "https://your-subdomain.onrender.com"   # URL pública de Render (https)
MP_ACCESS_TOKEN = "YOUR_MP_ACCESS_TOKEN"           # Access Token de Mercado Pago
# ===============================================

SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL   = "https://api.mercadopago.com/v1/payments/"

app = Flask(__name__)

# ---------- Texts ----------
WELCOME_MSG = (
    "🌹 *Welcome to PureMuse.*\n"
    "Where art meets sensuality.\n\n"
    "Choose one of the options below 👇"
)

ABOUT_MSG = (
    "*PureMuse* is a digital gallery where art and sensuality merge.\n"
    "Exclusive photographic collections, elegant aesthetics, and the beauty of desire.\n"
    "Discover. Feel. Collect."
)

COLLECTIONS_MSG = (
    "🖼️ *PureMuse Collections*\n"
    "• Noir & Gold Edition\n"
    "• Veils & Silhouettes\n"
    "• Amber Light\n"
    "_(Demo)_"
)

HELP_MSG = (
    "Available commands:\n"
    "/start, /hello, /about, /collections, /pay, /support, /help"
)

# ---------- Utilities ----------
def tg_send(chat_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(SEND_URL, json=payload, timeout=15)
    except Exception as e:
        app.logger.error(f"tg_send error: {e}")

def mp_create_preference(title="PureMuse VIP – 30 days", qty=1, unit_price=99.0, currency_id="MXN") -> str:
    """Creates a Mercado Pago checkout preference and returns the init_point URL."""
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
        "notification_url": f"{BASE_URL}/mp/webhook"
    }
    r = requests.post(MP_PREFS_URL, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("init_point") or data.get("sandbox_init_point")

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def home():
    return "PureMuse Bot is up ✨ vAutoWelcome", 200

# Telegram Webhook
@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    if request.method == "GET":
        return "Webhook OK", 200

    data = request.get_json(silent=True) or {}
    msg  = data.get("message") or data.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip().lower()

    if not chat_id or not text:
        return "OK", 200

    # When user starts the bot
    if text.startswith("/start"):
        keyboard = {
            "keyboard": [
                [{"text": "/about"}, {"text": "/collections"}],
                [{"text": "/pay"}, {"text": "/support"}]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False
        }
        tg_send(chat_id, WELCOME_MSG, reply_markup=keyboard)
    
    elif text.startswith("/about"):
        tg_send(chat_id, ABOUT_MSG)
    elif text.startswith("/collections"):
        tg_send(chat_id, COLLECTIONS_MSG)
    elif text.startswith("/help"):
        tg_send(chat_id, HELP_MSG)
    elif text.startswith("/pay"):
        try:
            url = mp_create_preference("PureMuse VIP – 30 days", 1, 99.0, "MXN")
            tg_send(
                chat_id,
                f"💎 *PureMuse VIP Access (30 days)*\n"
                "Price: $99 MXN\n\n"
                f"👉 [Pay now]({url})\n"
                "_Once your payment is confirmed, VIP access will be activated._"
            )
        except Exception as e:
            tg_send(chat_id, f"⚠️ Error creating payment link: {e}")
    elif text.startswith("/support"):
        tg_send(chat_id, "✉️ Support: contact@puremuse.example\nReplies within 24–48 hours.")
    else:
        tg_send(chat_id, "Unknown command. Use /help to see all available options.")

    return "OK", 200

# Entrypoint
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
