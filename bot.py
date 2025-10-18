from flask import Flask, request, jsonify
import requests

# ======= EDITA ESTAS 3 COSAS =======
TOKEN = "8280812701:AAGH4X-HoahE_jA6foiV0oo61CQrMuLd9hM"              # ej: 12345:ABC...
BASE_URL = "https://puremusebot.onrender.com"    # tu URL https ACTUAL 
MP_ACCESS_TOKEN = "APP_USR-3510033415376991-101723-4123f543520272287c00983a3ca15c83-95374565"        # tu Access Token de MP
# ====================================

SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL   = "https://api.mercadopago.com/v1/payments/"

app = Flask(__name__)

# ——— Textos en francés ———
START_FR = (
    "🌹 *PureMuse te souhaite la bienvenue.*\n"
    "Ici, l’art rencontre la sensualité.\n\n"
    "• /collections — Explorer les galeries\n"
    "• /about — Philosophie PureMuse\n"
    "• /buy — Accès VIP 30 jours\n"
    "• /help — Aide\n\n"
    "Laisse-toi guider par ta muse… ✨"
)

HOLA_FR = (
    "🌹 *Bienvenue chez PureMuse.*\n"
    "Plonge dans un univers de beauté, d’émotions et de mystère.\n\n"
    "• /collections — Explorer les galeries\n"
    "• /about — Philosophie PureMuse\n"
    "• /buy — Accès VIP 30 jours\n"
    "• /support — Contact\n"
    "💫 Ta muse t’attend."
)

ABOUT_FR = (
    "*PureMuse* est une galerie numérique où l’art et la sensualité s’unissent.\n"
    "Collections photographiques exclusives, esthétique élégante et désir suggéré.\n"
    "Découvre, ressens, collectionne."
)

COLLECTIONS_FR = (
    "🖼️ *Collections PureMuse*\n"
    "• Édition Noir & Or\n"
    "• Voiles & Silhouettes\n"
    "• Lumière d’Ambre\n"
    "_(Démo)_"
)

HELP_FR = (
    "Commandes disponibles:\n"
    "/start, /hola, /about, /collections, /buy, /support, /help"
)

def reply(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    )

# ——— Dentro de tu webhook ———
if text.startswith("/start"):
    reply(chat_id, START_FR)
    return jsonify({"ok": True})

if text.startswith("/hola"):
    reply(chat_id, HOLA_FR)
    return jsonify({"ok": True})

if text.startswith("/about"):
    reply(chat_id, ABOUT_FR)
    return jsonify({"ok": True})

if text.startswith("/collections"):
    reply(chat_id, COLLECTIONS_FR)
    return jsonify({"ok": True})

# Alias español y/o inglés para pagar
if text.startswith("/pagar") or text.startswith("/buy"):
    try:
        init_point, sandbox = mp_create_preference(
            title="PureMuse VIP – 30 jours",
            qty=1,
            unit_price=99.0,
            currency_id="MXN"
        )
        reply(chat_id,
              "💎 *Accès VIP PureMuse (30 jours)*\n"
              "Tarif: $99 MXN\n\n"
              f"👉 [Payer maintenant]({init_point})\n"
              "_Après le paiement, l’accès VIP sera activé._")
    except Exception as e:
        app.logger.error(f"/buy error: {e}")
        reply(chat_id, "⚠️ Erreur lors de la création du lien de paiement. Réessaie dans un moment.")
    return jsonify({"ok": True})

if text.startswith("/support"):
    reply(chat_id, "✉️ Support: contact@puremuse.example  \nRéponse sous 24–48h.")
    return jsonify({"ok": True})

if text.startswith("/help"):
    reply(chat_id, HELP_FR)
    return jsonify({"ok": True})

def tg_send(chat_id, text):
    requests.post(SEND_URL, json={"chat_id": chat_id, "text": text})

def mp_create_preference(title="Producto de prueba", qty=1, unit_price=10.0):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    body = {
        "items": [{"title": title, "quantity": qty, "unit_price": float(unit_price)}],
        "auto_return": "approved",
        "back_urls": {
            "success": f"{BASE_URL}/mp/return",
            "failure": f"{BASE_URL}/mp/return",
            "pending": f"{BASE_URL}/mp/return",
        },
        "notification_url": f"{BASE_URL}/mp/webhook"  # Webhook MP
    }
    r = requests.post(MP_PREFS_URL, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()  # devuelve init_point/sandbox_init_point
    return data.get("init_point") or data.get("sandbox_init_point")

def mp_get_payment(payment_id: str):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(MP_PAY_URL + str(payment_id), headers=headers, timeout=30)
    return r.json() if r.status_code == 200 else None

@app.route("/", methods=["GET"])
def home():
    return "OK", 200

# --- Webhook Telegram ---
# --- Webhook Telegram ---
@app.route("/webhook", methods=["POST","GET"])
def webhook():
    if request.method == "GET":
        return "Webhook OK", 200

    data = request.get_json(silent=True) or {}
    msg = data.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text", "")

    if not chat_id or not text:
        return "OK", 200

    # --- Respuestas en francés ---
    if text == "/start":
        tg_send(chat_id, START_FR)
    elif text == "/hola":
        tg_send(chat_id, HOLA_FR)
    elif text == "/about":
        tg_send(chat_id, ABOUT_FR)
    elif text == "/collections":
        tg_send(chat_id, COLLECTIONS_FR)
    elif text == "/help":
        tg_send(chat_id, HELP_FR)
    elif text in ["/pagar", "/buy"]:
        try:
            url = mp_create_preference("PureMuse VIP – 30 jours", 1, 99.0)
            tg_send(
                chat_id,
                f"💎 *Accès VIP PureMuse (30 jours)*\n"
                "Tarif: $99 MXN\n\n"
                f"👉 [Payer maintenant]({url})\n"
                "_Après le paiement, l’accès VIP sera activé._"
            )
        except Exception as e:
            tg_send(chat_id, f"⚠️ Erreur lors de la création du lien de paiement: {e}")
    elif text == "/support":
        tg_send(chat_id, "✉️ Support: contact@puremuse.example  \nRéponse sous 24–48h.")
    else:
        tg_send(chat_id, "Commande non reconnue. Utilise /help pour voir les options.")

    return "OK", 200


# --- Webhook Mercado Pago ---
@app.route("/mp/webhook", methods=["POST","GET"])
def mp_webhook():
    if request.method == "GET":
        return "MP Webhook OK", 200
    body = request.get_json(silent=True) or {}
    topic = body.get("type") or body.get("topic")
    payment_id = (body.get("data") or {}).get("id")
    if topic == "payment" and payment_id:
        pay = mp_get_payment(payment_id)  # GET /v1/payments/{id}
        if pay:
            status = pay.get("status")
            amount = pay.get("transaction_amount")
            email = (pay.get("payer") or {}).get("email", "sin_email")
            print(f"[MP] Pago {payment_id} -> {status} ${amount} {email}")
    return jsonify({"status":"received"}), 200

@app.route("/mp/return", methods=["GET"])
def mp_return():
    return "Gracias, estamos procesando tu pago.", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)





