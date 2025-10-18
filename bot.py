import os
import json
import random
from datetime import date, datetime, timedelta

from flask import Flask, request, jsonify
import requests

# === ENV ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "secret")

SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
SET_WEBHOOK_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL = "https://api.mercadopago.com/v1/payments/"

# === DB (SQLAlchemy 2.x) ===
from sqlalchemy import create_engine, BigInteger, Date, Integer, String, func, select
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class VIPUser(Base):
    __tablename__ = "vip_users"
    id = Integer().with_variant(Integer, "postgresql")
    id = Integer(primary_key=True, autoincrement=True)
    chat_id = BigInteger()
    username = String(150)
    start_date = Date()         # fecha de inicio VIP
    progress_day = Integer()    # día actual (0-29)
    # UNIQUE opcional según tu estrategia, aquí dejamos chat_id repetible por seguridad

# === APP ===
app = Flask(__name__)

# === UTIL ===
def tg_send(chat_id: int, text: str, parse_mode: str | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(SEND_URL, json=payload, timeout=15)
    except Exception:
        pass

def read_galleries() -> list[str]:
    """
    Lee galleries.txt (una URL por línea). Devuelve lista de 30 elementos.
    Si hay menos, se rellena con las últimas; si hay más, se usan las primeras 30.
    """
    path = os.path.join(os.getcwd(), "galleries.txt")
    links = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            links = [ln.strip() for ln in f.readlines() if ln.strip()]
    if not links:
        # fallback de seguridad
        links = [f"https://example.com/gallery/{i+1}" for i in range(30)]
    # normalizamos a 30
    if len(links) < 30:
        while len(links) < 30:
            links.append(links[-1])
    elif len(links) > 30:
        links = links[:30]
    return links

GALLERIES = read_galleries()

def is_active_vip(start: date, today: date | None = None) -> bool:
    if not today:
        today = date.today()
    return (today - start).days < 30

def ensure_tables():
    Base.metadata.create_all(bind=engine)

def get_or_create_vip(chat_id: int, username: str | None = None) -> VIPUser:
    with SessionLocal() as db:
        u = db.execute(
            select(VIPUser).where(VIPUser.chat_id == chat_id)
        ).scalar_one_or_none()
        if u:
            if username and (not u.username):
                u.username = username
                db.commit()
            return u
        u = VIPUser(chat_id=chat_id, username=username or "", start_date=date.today(), progress_day=0)
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

def set_vip_start_or_refresh(chat_id: int, username: str | None = None):
    """
    Si ya era VIP, reinicia a día 0 desde hoy. Si no existía, lo crea.
    """
    with SessionLocal() as db:
        u = db.execute(
            select(VIPUser).where(VIPUser.chat_id == chat_id)
        ).scalar_one_or_none()
        if u:
            u.start_date = date.today()
            u.progress_day = 0
            if username and not u.username:
                u.username = username
            db.commit()
            return u
        u = VIPUser(chat_id=chat_id, username=username or "", start_date=date.today(), progress_day=0)
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

def send_gallery_today(vip: VIPUser):
    """
    Envía la galería correspondiente al progress_day del usuario.
    No incrementa; el incremento se hace aparte para controlar reintentos.
    """
    day = max(0, min(vip.progress_day, 29))
    link = GALLERIES[day]
    msg = f"🎁 *PureMuse VIP – Día {day+1}/30*\n\nTu galería de hoy:\n{link}\n\n¡Disfrútala!"
    tg_send(vip.chat_id, msg, parse_mode="Markdown")

def increment_progress(vip: VIPUser):
    with SessionLocal() as db:
        u = db.execute(
            select(VIPUser).where(VIPUser.id == vip.id)
        ).scalar_one()
        u.progress_day = min(29, (u.progress_day or 0) + 1)
        db.commit()

def mp_create_link(chat_id: int) -> str:
    """
    Crea el link de pago con external_reference = chat_id
    """
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    # Ajusta los datos del item y precios a tu oferta
    data = {
        "items": [
            {
                "title": "PureMuse VIP – 30 días",
                "quantity": 1,
                "unit_price": 99.0,
                "currency_id": "MXN"
            }
        ],
        "external_reference": str(chat_id),
        "notification_url": f"{BASE_URL}/mp/webhook?secret={MP_WEBHOOK_SECRET}",
        "back_urls": {
            "success": f"{BASE_URL}/paid?status=success",
            "pending": f"{BASE_URL}/paid?status=pending",
            "failure": f"{BASE_URL}/paid?status=failure"
        },
        "auto_return": "approved"
    }
    r = requests.post(MP_PREFS_URL, headers=headers, json=data, timeout=20)
    r.raise_for_status()
    payload = r.json()
    # init_point (web) o sandbox_init_point (sandbox). En prod es init_point.
    return payload.get("init_point") or payload.get("sandbox_init_point")

def mp_fetch_payment(payment_id: str) -> dict | None:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    url = MP_PAY_URL + payment_id
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code == 200:
        return r.json()
    return None

# === ROUTES ===
@app.route("/", methods=["GET"])
def root():
    return jsonify({"ok": True, "service": "PureMuse Bot"}), 200

@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    if not BASE_URL:
        return jsonify({"ok": False, "error": "Set BASE_URL env var"}), 400
    url = f"{BASE_URL}/telegram"
    r = requests.get(SET_WEBHOOK_URL, params={"url": url}, timeout=15)
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify({"ok": False, "raw": r.text}), r.status_code

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    data = request.get_json(silent=True) or {}
    message = data.get("message") or data.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    username = message.get("from", {}).get("username")

    if not text:
        return jsonify({"ok": True})

    # Comandos
    if text.startswith("/start"):
        # Ofrecer link de pago + explicar VIP
        pay_link = mp_create_link(chat_id)
        tg_send(chat_id,
                "💎 *PureMuse VIP*\n\nAcceso a 30 galerías exclusivas (1 por día).\n\n"
                f"Precio: $99 MXN\n\nPaga aquí:\n{pay_link}",
                parse_mode="Markdown")
        return jsonify({"ok": True})

    if text.startswith("/testdb"):
        # Crea tablas si no existen
        ensure_tables()
        tg_send(chat_id, "✅ DB lista (tablas creadas/verificadas).")
        return jsonify({"ok": True})

    if text.startswith("/vipstatus"):
        with SessionLocal() as db:
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if not u:
                tg_send(chat_id, "❌ No tienes VIP activo. Usa /start para obtener acceso.")
            else:
                active = is_active_vip(u.start_date)
                dias = 30 - (date.today() - u.start_date).days
                dias = max(0, dias)
                tg_send(chat_id, f"👤 VIP: {'ACTIVO' if active else 'VENCIDO'}\n"
                                 f"Inicio: {u.start_date}\n"
                                 f"Día actual: {u.progress_day+1}/30\n"
                                 f"Días restantes: {dias}")
        return jsonify({"ok": True})

    if text.startswith("/sendtoday"):
        if chat_id != ADMIN_CHAT_ID:
            tg_send(chat_id, "⛔ Solo admin.")
            return jsonify({"ok": True})
        # Fuerza el envío del día de hoy para todos los VIP activos (sin incrementar)
        with SessionLocal() as db:
            rows = db.execute(select(VIPUser)).scalars().all()
            sent = 0
            for u in rows:
                if is_active_vip(u.start_date):
                    send_gallery_today(u)
                    sent += 1
        tg_send(chat_id, f"📨 Envío manual de hoy realizado a {sent} VIP(s).")
        return jsonify({"ok": True})

    # fallback
    tg_send(chat_id, "Comandos disponibles:\n/start – Comprar VIP\n/vipstatus – Estado VIP\n/testdb – Verificar DB\n/sendtoday – (admin)")
    return jsonify({"ok": True})

@app.route("/mp/webhook", methods=["POST", "GET"])
def mp_webhook():
    """
    Mercado Pago envía notificaciones con ?type=payment & data.id=PAYMENT_ID
    Validamos "?secret=" para evitar ruidos.
    """
    secret = request.args.get("secret")
    if secret != MP_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    payload = request.get_json(silent=True) or {}
    # MP manda varias formas; contemplamos las comunes:
    type_ = request.args.get("type") or payload.get("type")
    data_id = request.args.get("data.id") or (payload.get("data", {}) or {}).get("id")

    if type_ == "payment" and data_id:
        info = mp_fetch_payment(str(data_id))
        if info and info.get("status") == "approved":
            # Recuperamos chat_id desde external_reference
            ext = info.get("external_reference")
            try:
                chat_id = int(ext)
            except Exception:
                chat_id = None
            payer_username = None
            # Si quieres, aquí puedes intentar extraer email/nombre.
            if chat_id:
                ensure_tables()
                u = set_vip_start_or_refresh(chat_id, payer_username)
                tg_send(chat_id, "✅ *Pago aprobado.* VIP activado por 30 días.\n\nTu primera galería llega en breve.",
                        parse_mode="Markdown")
                # Enviar primera galería
                send_gallery_today(u)
                increment_progress(u)
        return jsonify({"ok": True})

    # Otras notificaciones las ignoramos
    return jsonify({"ok": True})

@app.route("/cron/daily", methods=["GET"])
def cron_daily():
    """
    Endpoint para CRON DIARIO (Render).
    Envío 1 galería y avanzo progress_day.
    Idempotente por día si tu CRON corre 1 vez/día.
    """
    ensure_tables()
    today = date.today()
    sent = 0
    with SessionLocal() as db:
        users = db.execute(select(VIPUser)).scalars().all()
        for u in users:
            if is_active_vip(u.start_date, today):
                # Enviar galería del día actual de su progreso
                send_gallery_today(u)
                increment_progress(u)
                sent += 1
    return jsonify({"ok": True, "sent": sent, "date": str(today)}), 200

@app.route("/paid", methods=["GET"])
def paid_landing():
    status = request.args.get("status", "unknown")
    return f"Pago: {status}. Puedes cerrar esta pestaña y volver a Telegram.", 200

# === STARTUP: crear tablas al iniciar en Render ===
with app.app_context():
    ensure_tables()
