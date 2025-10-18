from flask import Flask, request, jsonify
import requests

# ======= EDITA ESTAS 3 COSAS =======
TOKEN = "8280812701:AAGH4X-HoahE_jA6foiV0oo61CQrMuLd9hM"              # ej: 12345:ABC...
BASE_URL = "https://8f5d79b2f806.ngrok-free.app"    # tu URL https ACTUAL de ngrok
MP_ACCESS_TOKEN = "APP_USR-3510033415376991-101723-4123f543520272287c00983a3ca15c83-95374565"        # tu Access Token de MP
# ====================================

SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL   = "https://api.mercadopago.com/v1/payments/"

app = Flask(__name__)

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
@app.route("/webhook", methods=["POST","GET"])
def webhook():
    if request.method == "GET":
        return "Webhook OK", 200
    data = request.get_json(silent=True) or {}
    msg = data.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text", "")

    if chat_id and text == "/start":
        tg_send(chat_id, "✅ Bot activo.\nEnvía /pagar para link de pago.")
    elif chat_id and text == "/pagar":
        try:
            url = mp_create_preference("Compra de prueba", 1, 99.0)
            tg_send(chat_id, f"💳 Paga aquí:\n{url}")
        except Exception as e:
            tg_send(chat_id, f"⚠️ Error creando pago: {e}")
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

