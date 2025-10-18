import os
import json
from datetime import date
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
from sqlalchemy import create_engine, BigInteger, Integer, String, Date, select, UniqueConstraint
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

class Base(DeclarativeBase):
    pass

class VIPUser(Base):
    __tablename__ = "vip_users"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_vip_chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)  # inicio VIP
    progress_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0–29

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
    path = os.path.join(os.getcwd(), "galleries.txt")
    links = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            links = [ln.strip() for ln in f if ln.strip()]
    if not links:
        links = [f"https://example.com/gallery/{i+1}" for i in range(30)]
    if len(links) < 30:
        while len(links) < 30:
            links.append(links[-1])
    elif len(links) > 30:
        links = links[:30]
    return links

GALLERIES = read_galleries()

def is_active_vip(start: date, today: date | None = None) -> bool:
    if today is None:
        today = date.today()
    return (today - start).days < 30

def ensure_tables():
    Base.metadata.create_all(bind=engine)

def get_or_create_vip(chat_id: int, username: str | None = None) -> VIPUser:
    with SessionLocal() as db:
        u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
        if u:
            if username and not u.username:
                u.username = username
                db.commit()
            return u
        u = VIPUser(chat_id=chat_id, username=username or "", start_date=date.today(), progress_day=0)
        db.add(u); db.commit(); db.refresh(u)
        return u

def set_vip_start_or_refresh(chat_id: int, username: str | None = None):
    with SessionLocal() as db:
        u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
        if u:
            u.start_date = date.today()
            u.progress_day = 0
            if username and not u.username:
                u.username = username
            db.commit()
            return u
        u = VIPUser(chat_id=chat_id, username=username or "", start_date=date.today(), progress_day=0)
        db.add(u); db.commit(); db.refresh(u)
        return u

def send_gallery_today(vip: VIPUser):
    day = max(0, min(vip.progress_day, 29))
    link = GALLERIES[day]
    msg = f"🎁 *PureMuse VIP – Día {day+1}/30*\n\nTu galería de hoy:\n{link}\n\n¡Disfrútala!"
    tg_send(vip.chat_id, msg, parse_mode="Markdown")

def increment_progress(vip: VIPUser):
    with SessionLocal() as db:
        u = db.get(VIPUser, vip.id)
        u.progress_day = min(29, (u.progress_day or 0) + 1)
        db.commit()

def mp_create_link(chat_id: int) -> str:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "items": [{"title": "PureMuse VIP – 30 días", "quantity": 1, "unit_price": 99.0, "currency_id": "MXN"}],
        "external_reference": str(chat_id),
        "notification_url": f"{BASE_URL}/mp/webhook?secret={MP_WEBHOOK_SECRET}",
        "back_urls": {
            "success": f"{BASE_URL}/paid?status=success",
            "pending": f"{BASE_URL}/paid?status=pending",
            "failure": f"{BASE_URL}/paid?status=failure",
        },
        "auto_return": "approved",
    }
    r = requests.post(MP_PREFS_URL, headers=headers, json=data, timeout=20)
    r.raise_for_status()
    payload = r.json()
    return payload.get("init_point") or payload.get("sandbox_init_point")

def mp_fetch_payment(payment_id: str) -> dict | None:
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    url = MP_PAY_URL + payment_id
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code == 200:
        return r.json()
    return None

# === ROUTES ===
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "PureMuse Bot"}), 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/set_webhook")
def set_webhook():
    if not BASE_URL:
        return jsonify({"ok": False, "error": "Set BASE_URL env var"}), 400
    url = f"{BASE_URL}/telegram"
    r = requests.get(SET_WEBHOOK_URL, params={"url": url}, timeout=15)
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify({"ok": False, "raw": r.text}), r.status_code

@app.post("/telegram")
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

    if text.startswith("/start"):
        pay_link = mp_create_link(chat_id)
        tg_send(chat_id,
                "💎 *PureMuse VIP*\n\nAcceso a 30 galerías exclusivas (1 por día).\n\n"
                f"Precio: $99 MXN\n\nPaga aquí:\n{pay_link}",
                parse_mode="Markdown")
        return jsonify({"ok": True})

    if text.startswith("/testdb"):
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
                dias = max(0, 30 - (date.today() - u.start_date).days)
                tg_send(chat_id, f"👤 VIP: {'ACTIVO' if active else 'VENCIDO'}\n"
                                 f"Inicio: {u.start_date}\n"
                                 f"Día actual: {u.progress_day+1}/30\n"
                                 f"Días restantes: {dias}")
        return jsonify({"ok": True})

    if text.startswith("/sendtoday"):
        if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
            tg_send(chat_id, "⛔ Solo admin.")
            return jsonify({"ok": True})
        with SessionLocal() as db:
            rows = db.execute(select(VIPUser)).scalars().all()
            sent = 0
            for u in rows:
                if is_active_vip(u.start_date):
                    send_gallery_today(u)
                    sent += 1
        tg_send(chat_id, f"📨 Envío manual de hoy realizado a {sent} VIP(s).")
        return jsonify({"ok": True})

    tg_send(chat_id, "Comandos:\n/start – Comprar VIP\n/vipstatus – Estado VIP\n/testdb – Verificar DB\n/sendtoday – (admin)")
    return jsonify({"ok": True})

@app.route("/mp/webhook", methods=["POST", "GET"])
def mp_webhook():
    secret = request.args.get("secret")
    if secret != MP_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    payload = request.get_json(silent=True) or {}
    type_ = request.args.get("type") or payload.get("type")
    data_id = request.args.get("data.id") or (payload.get("data", {}) or {}).get("id")

    if type_ == "payment" and data_id:
        info = mp_fetch_payment(str(data_id))
        if info and info.get("status") == "approved":
            ext = info.get("external_reference")
            try:
                chat_id = int(ext)
            except Exception:
                chat_id = None
            if chat_id:
                ensure_tables()
                u = set_vip_start_or_refresh(chat_id, None)
                tg_send(chat_id, "✅ *Pago aprobado.* VIP activado por 30 días.\n\nTu primera galería llega en breve.",
                        parse_mode="Markdown")
                send_gallery_today(u)
                increment_progress(u)
    return jsonify({"ok": True})

@app.get("/cron/daily")
def cron_daily():
    ensure_tables()
    sent = 0
    with SessionLocal() as db:
        users = db.execute(select(VIPUser)).scalars().all()
        for u in users:
            if is_active_vip(u.start_date):
                send_gallery_today(u)
                increment_progress(u)
                sent += 1
    return jsonify({"ok": True, "sent": sent, "date": str(date.today())}), 200

@app.get("/paid")
def paid_landing():
    status = request.args.get("status", "unknown")
    return f"Pago: {status}. Puedes cerrar esta pestaña y volver a Telegram.", 200

# Crear tablas al arrancar
with app.app_context():
    ensure_tables()
