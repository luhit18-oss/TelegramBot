# =========================================================
# PureMuse Telegram Bot - Render + Flask + SQLAlchemy
# Vars usadas (Render): TOKEN, BASE_URL, MP_ACCESS_TOKEN, CRON_TOKEN, DATABASE_URL
# Teclado persistente (Reply Keyboard) con botones en inglés:
#   Pure Muse | VIP
#   Galleries | VIP status
# Lógica:
# - VIP: $50 MXN / 30 días via Mercado Pago (webhook protegido con CRON_TOKEN)
# - Envío diario 00:00 America/Mexico_City con cron job: /cron/daily?secret=CRON_TOKEN
# - No repetir galerías por usuario (hash por URL), historial en DB (Neon)
# - Limpieza: borra usuarios inactivos 12 meses
# =========================================================

import os
import html
import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import requests
from flask import Flask, request, jsonify

# ========= ENV VARS (Render) =========
TOKEN = os.getenv("TOKEN", "")
BASE_URL = (os.getenv("BASE_URL", "") or "").rstrip("/")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Telegram endpoints
TG_BASE = f"https://api.telegram.org/bot{TOKEN}"
TG_SEND_URL = f"{TG_BASE}/sendMessage"
TG_SET_WEBHOOK_URL = f"{TG_BASE}/setWebhook"
TG_GET_WEBHOOK_INFO_URL = f"{TG_BASE}/getWebhookInfo"

# Mercado Pago endpoints
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL = "https://api.mercadopago.com/v1/payments/"

# Zona horaria MX
TZ_MX = ZoneInfo("America/Mexico_City")

# ========= DB (SQLAlchemy 2.x) =========
from sqlalchemy import (
    create_engine, BigInteger, Integer, String, Date, DateTime, select, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column

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
    username: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    active_until: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=False), nullable=True)

class VIPDelivery(Base):
    __tablename__ = "vip_deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    gallery_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

def ensure_tables():
    Base.metadata.create_all(bind=engine)

# ========= Utilidades =========
def esc_html(s: str) -> str:
    return html.escape(s, quote=True)

def build_reply_keyboard() -> dict:
    keyboard = [
        [{"text": "Pure Muse"}, {"text": "VIP"}],
        [{"text": "Galleries"}, {"text": "VIP status"}],
    ]
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": True,
        "input_field_placeholder": "Choose an option…",
    }

def tg_send_text(chat_id: int, text: str, disable_preview: bool = False, reply_markup: Optional[dict] = None):
    if not TOKEN:
        print("⚠️ TOKEN vacío; no se envió mensaje")
        return
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(TG_SEND_URL, json=payload, timeout=15)
        if r.status_code != 200:
            print("⚠️ Error sendMessage:", r.status_code, r.text)
    except Exception as e:
        print("⚠️ Error al enviar Telegram:", e)

def read_galleries() -> list[str]:
    path = os.path.join(os.getcwd(), "galleries.txt")
    links: list[str] = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            links = [ln.strip() for ln in f if ln.strip()]
    return links

def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()

def now_mx() -> datetime:
    return datetime.now(tz=TZ_MX)

def is_vip_active(u: VIPUser) -> bool:
    return now_mx() < u.active_until

def vip_days_left(u: VIPUser) -> int:
    delta = u.active_until - now_mx()
    return max(0, delta.days)

def normalize_command(raw: str) -> str:
    t = (raw or "").strip()
    if t.startswith("/"):
        t = t.split()[0]
        t = t.split("@")[0]
        t = t.lstrip("/")
    return t.upper()

def mp_create_link(chat_id: int) -> str:
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN is empty")
    if not BASE_URL:
        raise RuntimeError("BASE_URL is empty")
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "items": [{"title": "PureMuse VIP – 30 days", "quantity": 1, "unit_price": 50.0, "currency_id": "MXN"}],
        "external_reference": str(chat_id),
        "notification_url": f"{BASE_URL}/mp/webhook?secret={CRON_TOKEN}",
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

def mp_fetch_payment(payment_id: str) -> Optional[dict]:
    if not MP_ACCESS_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    url = MP_PAY_URL + str(payment_id)
    r = requests.get(url, headers=headers, timeout=20)
    if r.status_code == 200:
        return r.json()
    print("⚠️ MP fetch payment error:", r.status_code, r.text)
    return None

def pick_next_gallery_for_user(db, chat_id: int) -> Optional[str]:
    """
    Devuelve la primera URL en galleries.txt que el usuario NO haya recibido nunca.
    Si no hay nuevas, devuelve None.
    """
    links = read_galleries()
    if not links:
        return None
    # Carga hashes ya enviados
    sent_hashes = {
        row[0]
        for row in db.execute(
            select(VIPDelivery.gallery_hash).where(VIPDelivery.chat_id == chat_id)
        )
    }
    for url in links:
        h = url_hash(url)
        if h not in sent_hashes:
            return url
    return None

def record_delivery(db, chat_id: int, url: str):
    h = url_hash(url)
    db.add(VIPDelivery(
        chat_id=chat_id,
        gallery_hash=h,
        url=url,
        sent_at=now_mx().replace(tzinfo=None),
    ))

# ========= Flask app & routes =========
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify({"ok": True, "service": "PureMuse Bot"}), 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/testdb")
def testdb():
    try:
        ensure_tables()
        return "✅ DB ready.", 200
    except Exception as e:
        return f"❌ DB error: {e}", 500

@app.get("/set_webhook")
def set_webhook():
    if not BASE_URL or not TOKEN:
        return jsonify({"ok": False, "error": "Set BASE_URL and TOKEN"}), 400
    url = f"{BASE_URL}/telegram"
    r = requests.get(TG_SET_WEBHOOK_URL, params={"url": url}, timeout=15)
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify({"ok": False, "raw": r.text}), r.status_code

@app.get("/get_webhook_info")
def get_webhook_info():
    if not TOKEN:
        return jsonify({"ok": False, "error": "Set TOKEN"}), 400
    r = requests.get(TG_GET_WEBHOOK_INFO_URL, timeout=15)
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify({"ok": False, "raw": r.text}), r.status_code

@app.get("/paid")
def paid():
    status = request.args.get("status", "unknown")
    return f"Payment status: {status}", 200

# ===== Mercado Pago Webhook (activar VIP) =====
@app.post("/mp/webhook")
def mp_webhook():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    payload = request.get_json(silent=True) or {}
    payment_id = (payload.get("data") or {}).get("id") or payload.get("id") or payload.get("resource", "").split("/")[-1]
    if not payment_id:
        return "ok", 200
    pay = mp_fetch_payment(str(payment_id))
    if not pay:
        return "ok", 200

    status = pay.get("status")
    amount = pay.get("transaction_amount")
    currency = pay.get("currency_id")
    ext_ref = pay.get("external_reference")
    try:
        chat_id = int(ext_ref) if ext_ref else None
    except Exception:
        chat_id = None

    if status == "approved" and chat_id and amount == 50 and currency == "MXN":
        try:
            ensure_tables()
            with SessionLocal() as db:
                today = now_mx().date()
                active_until = (datetime.now(tz=TZ_MX) + timedelta(days=30)).replace(tzinfo=None)
                u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
                if u:
                    u.start_date = today
                    u.active_until = active_until
                    u.last_sent_at = None
                else:
                    u = VIPUser(chat_id=chat_id, username=None, start_date=today, active_until=active_until, last_sent_at=None)
                    db.add(u)
                db.commit()
            tg_send_text(chat_id, "✅ <b>Payment approved.</b> Your VIP is now active for 30 days.", reply_markup=build_reply_keyboard())
        except Exception as e:
            print("⚠️ Error activating VIP:", e)
    return "ok", 200

# ===== Telegram Webhook =====
@app.post("/telegram")
def telegram_webhook():
    data = request.get_json(silent=True) or {}

    # callback_query (no usamos inline aquí)
    if data.get("callback_query"):
        return jsonify({"ok": True})

    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return jsonify({"ok": True})

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not text or not chat_id:
        return jsonify({"ok": True})

    t = normalize_command(text)

    # /start
    if t in ("START", "MENU"):
        welcome = (
            "✨ <b>Welcome to PureMuse Bot</b>\n\n"
            "Use the buttons below to navigate.\n"
            "VIP grants you 30 days of daily gallery links."
        )
        tg_send_text(chat_id, welcome, reply_markup=build_reply_keyboard())
        return jsonify({"ok": True})

    # "Pure Muse"
    if text == "Pure Muse":
        tg_send_text(
            chat_id,
            "👋 <b>Pure Muse</b>\n\nArtistic premium galleries. With VIP you get one new private link every day for 30 days.",
            reply_markup=build_reply_keyboard(),
            disable_preview=True,
        )
        return jsonify({"ok": True})

    # "VIP" (crear link de pago)
    if text == "VIP":
        try:
            link = mp_create_link(chat_id)
            msg = (
                "💳 <b>VIP Access</b>\n\nPrice: <b>$50 MXN</b> for <b>30 days</b>.\n"
                f"Complete your payment here:\n{esc_html(link)}\n\n"
                "Once approved, your VIP will be activated automatically."
            )
            tg_send_text(chat_id, msg, reply_markup=build_reply_keyboard(), disable_preview=True)
        except Exception as e:
            tg_send_text(chat_id, f"⚠️ Could not create payment link. Try again in a moment.\n\n{esc_html(str(e))}", reply_markup=build_reply_keyboard())
        return jsonify({"ok": True})

    # "Galleries" (si VIP activo, envía la próxima no repetida; si no, pide comprar)
    if text == "Galleries":
        try:
            ensure_tables()
            with SessionLocal() as db:
                u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
                if not u or not is_vip_active(u):
                    tg_send_text(chat_id, "🔒 VIP required. Get access via <b>VIP</b> button.", reply_markup=build_reply_keyboard())
                else:
                    # Enviar solo si aún no se envió hoy
                    today = now_mx().date()
                    already_sent_today = u.last_sent_at and u.last_sent_at.date() == today
                    if already_sent_today:
                        tg_send_text(chat_id, "✅ You already received today’s gallery. Come back tomorrow 😊", reply_markup=build_reply_keyboard(), disable_preview=True)
                    else:
                        url = pick_next_gallery_for_user(db, chat_id)
                        if not url:
                            tg_send_text(chat_id, "ℹ️ No new galleries available at the moment.", reply_markup=build_reply_keyboard())
                        else:
                            tg_send_text(chat_id, f"🎁 <b>Your gallery today</b>:\n{esc_html(url)}", reply_markup=build_reply_keyboard(), disable_preview=False)
                            record_delivery(db, chat_id, url)
                            u.last_sent_at = now_mx().replace(tzinfo=None)
                            db.commit()
        except Exception as e:
            tg_send_text(chat_id, f"⚠️ Error while retrieving galleries.\n{esc_html(str(e))}", reply_markup=build_reply_keyboard())
        return jsonify({"ok": True})

    # "VIP status"
    if text == "VIP status":
        try:
            ensure_tables()
            with SessionLocal() as db:
                u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
                if not u:
                    tg_send_text(chat_id, "❌ No active VIP. Use <b>VIP</b> to subscribe.", reply_markup=build_reply_keyboard())
                else:
                    days_left = vip_days_left(u)
                    status_txt = "ACTIVE ✅" if is_vip_active(u) else "EXPIRED ❌"
                    tg_send_text(
                        chat_id,
                        (
                            f"👤 <b>VIP Status</b>\n\n"
                            f"Status: <b>{status_txt}</b>\n"
                            f"Start: {esc_html(u.start_date.isoformat())}\n"
                            f"Days left: {days_left}\n"
                        ),
                        reply_markup=build_reply_keyboard(),
                    )
        except Exception as e:
            tg_send_text(chat_id, f"⚠️ Error checking VIP status.\n{esc_html(str(e))}", reply_markup=build_reply_keyboard())
        return jsonify({"ok": True})

    # Fallback → mostrar teclado
    tg_send_text(chat_id, "Choose an option:", reply_markup=build_reply_keyboard())
    return jsonify({"ok": True})

# ===== Cron diario (00:00 MX) =====
@app.post("/cron/daily")
def cron_daily():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403

    # Solo corre si ya estamos en día nuevo MX
    now = now_mx()
    if now.hour != 0:
        return jsonify({"ok": True, "skipped": "not midnight"}), 200

    ensure_tables()
    sent_users = 0
    cleaned_users = 0
    with SessionLocal() as db:
        # Limpieza: eliminar usuarios inactivos por 12 meses
        twelve_months_ago = (now - timedelta(days=365)).date()
        users = list(db.execute(select(VIPUser)).scalars())
        for u in users:
            if u.active_until.date() < twelve_months_ago:
                # borrar sus deliveries y su user
                db.query(VIPDelivery).filter(VIPDelivery.chat_id == u.chat_id).delete()
                db.query(VIPUser).filter(VIPUser.chat_id == u.chat_id).delete()
                cleaned_users += 1
        db.commit()

        # Enviar a quienes tengan VIP activo y no se haya enviado hoy
        users = list(db.execute(select(VIPUser)).scalars())
        for u in users:
            if not is_vip_active(u):
                continue
            already_sent_today = u.last_sent_at and u.last_sent_at.date() == now.date()
            if already_sent_today:
                continue
            url = pick_next_gallery_for_user(db, u.chat_id)
            if not url:
                # Sin nuevas galerías; notificamos una sola vez
                tg_send_text(u.chat_id, "ℹ️ No new galleries available today. Please check back later.", reply_markup=build_reply_keyboard())
                continue
            tg_send_text(u.chat_id, f"🎁 <b>Your gallery today</b>:\n{esc_html(url)}", reply_markup=build_reply_keyboard(), disable_preview=False)
            record_delivery(db, u.chat_id, url)
            u.last_sent_at = now.replace(tzinfo=None)
            db.commit()
            sent_users += 1

    return jsonify({"ok": True, "sent": sent_users, "cleaned": cleaned_users}), 200

# ===== Recargar galleries.txt sin redeploy (opcional) =====
@app.post("/reload_galleries")
def reload_galleries():
    # protegido por header
    if request.headers.get("X-Admin-Secret") != CRON_TOKEN:
        return "forbidden", 403
    # No necesitamos hacer nada si leemos el archivo cada vez,
    # pero dejamos el endpoint por si quieres forzar pruebas / monitoreo
    links = read_galleries()
    return jsonify({"ok": True, "count": len(links)}), 200

# ===== Main local =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
