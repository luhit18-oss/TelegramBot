# =========================================================
# PureMuse Telegram Bot – Version 3.2-stable
# ---------------------------------------------------------
# Features:
#  🌹 Free + VIP Galleries
#  💳 MercadoPago integration
#  🕛 Daily delivery & expiration
#  🔒 DB auto-repair and persistence
#  💬 Owner alerts for any DB or API issue
# =========================================================

import os
import html
import hashlib
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Iterable

import requests
from flask import Flask, request, jsonify
from sqlalchemy import (
    create_engine, BigInteger, Integer, String, Date, DateTime,
    select, UniqueConstraint, func, text
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column
from sqlalchemy.exc import ProgrammingError, OperationalError

# ========= ENV VARS =========
TOKEN = os.getenv("TOKEN", "")
BASE_URL = (os.getenv("BASE_URL", "") or "").rstrip("/")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
OWNER_CHAT_ID = 1703988973  # <— tu chat_id (notificaciones al dueño)

# ========= CONSTANTES =========
TG_BASE = f"https://api.telegram.org/bot{TOKEN}"
TG_SEND_URL = f"{TG_BASE}/sendMessage"
TG_SET_WEBHOOK_URL = f"{TG_BASE}/setWebhook"
TG_GET_WEBHOOK_INFO_URL = f"{TG_BASE}/getWebhookInfo"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL = "https://api.mercadopago.com/v1/payments/"
TZ_MX = ZoneInfo("America/Mexico_City")

# ========= DATABASE =========
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

class Base(DeclarativeBase):
    pass

class VIPUser(Base):
    __tablename__ = "vip_users"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_vip_chat_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    active_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

class VIPDelivery(Base):
    __tablename__ = "vip_deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    gallery_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

def ensure_schema_safe():
    """Garantiza que las tablas existan, incluso si Neon tarda o falla."""
    try:
        Base.metadata.create_all(bind=engine)
        return True
    except Exception as e:
        print("⚠️ ensure_schema_safe error:", e)
        notify_owner(f"⚠️ DB schema check failed:\n<pre>{esc(str(e))}</pre>")
        return False

# ========= UTILS =========
def esc(s: str) -> str:
    return html.escape(s, quote=True)

def now_mx() -> datetime:
    return datetime.now(tz=TZ_MX)

def day_mx() -> date:
    return now_mx().date()

def url_hash(u: str) -> str:
    return hashlib.sha256(u.encode("utf-8")).hexdigest()

def read_galleries() -> list[str]:
    path = os.path.join(os.getcwd(), "galleries.txt")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

def is_active(u: VIPUser) -> bool:
    return now_mx() < u.active_until

def days_left(u: VIPUser) -> int:
    return max(0, (u.active_until - now_mx()).days)

def build_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Pure Muse"}, {"text": "VIP"}],
            [{"text": "Galleries"}, {"text": "VIP status"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Choose an option…",
    }

def tg_send(chat_id: int, text: str, preview=False, kb=True):
    if not TOKEN:
        return
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": not preview,
    }
    if kb:
        payload["reply_markup"] = build_keyboard()
    try:
        requests.post(TG_SEND_URL, json=payload, timeout=15)
    except Exception as e:
        print("⚠️ Telegram send exception:", e)

def notify_owner(text: str):
    try:
        tg_send(OWNER_CHAT_ID, text, preview=False, kb=False)
    except Exception as e:
        print("⚠️ Owner notify error:", e)

# ========= Mercado Pago =========
def mp_create_link(chat_id: int) -> str:
    if not MP_ACCESS_TOKEN or not BASE_URL:
        raise RuntimeError("Missing MP_ACCESS_TOKEN or BASE_URL")
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "items": [{"title": "PureMuse VIP – 30 days", "quantity": 1, "unit_price": 50.0, "currency_id": "MXN"}],
        "external_reference": str(chat_id),
        "notification_url": f"{BASE_URL}/mp/webhook?secret={CRON_TOKEN}",
        "back_urls": {"success": f"{BASE_URL}/paid?status=success"},
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
    r = requests.get(MP_PAY_URL + str(payment_id), headers=headers, timeout=20)
    if r.status_code == 200:
        return r.json()
    return None

# ========= Gallery Logic =========
def pick_new_from_pool(db, chat_id: int, pool: Iterable[str]) -> Optional[str]:
    sent_hashes = {row[0] for row in db.execute(select(VIPDelivery.gallery_hash).where(VIPDelivery.chat_id == chat_id))}
    for link in pool:
        if url_hash(link) not in sent_hashes:
            return link
    return None

def pick_vip_gallery(db, chat_id: int) -> Optional[str]:
    links = read_galleries()
    if len(links) <= 1:
        return None
    return pick_new_from_pool(db, chat_id, links[1:])

def record_delivery(db, chat_id: int, url: str):
    db.add(VIPDelivery(chat_id=chat_id, gallery_hash=url_hash(url), url=url, sent_at=now_mx().replace(tzinfo=None)))

# ========= Flask App =========
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify(ok=True, service="PureMuse Bot v3.2"), 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/testdb")
def testdb():
    ensure_schema_safe()
    return "✅ DB ready", 200

# ========= Telegram Webhook =========
@app.post("/telegram")
def telegram_webhook():
    ensure_schema_safe()
    d = request.get_json(silent=True) or {}
    msg = d.get("message") or d.get("edited_message")
    if not msg:
        return jsonify(ok=True)

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return jsonify(ok=True)

    txt = (msg.get("text") or "").strip()

    try:
        if txt.lower() in ("/start", "menu"):
            tg_send(chat_id, "✨ <b>Welcome to Pure Muse</b>\n\nWhere art meets desire. Try your <b>first gallery for free</b> 🌹\nUnlock <b>30 days</b> of private beauty with <b>VIP</b> 💋")
            return jsonify(ok=True)

        if txt == "Pure Muse":
            tg_send(chat_id, "🌹 <b>Pure Muse</b>\n\nArtistic sensuality. Use <b>VIP</b> to awaken your muse 🔥")
            return jsonify(ok=True)

        if txt == "VIP":
            link = mp_create_link(chat_id)
            tg_send(chat_id, f"💳 <b>VIP Access</b>\n\n<b>$50 MXN</b> for <b>30 days</b>.\nComplete your payment here:\n{esc(link)} 💋")
            return jsonify(ok=True)

        if txt == "VIP status":
            ensure_schema_safe()
            with SessionLocal() as db:
                try:
                    u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
                except (ProgrammingError, OperationalError):
                    ensure_schema_safe()
                    u = None
                if not u:
                    tg_send(chat_id, "❌ No VIP found. Tap <b>VIP</b> to begin ✨")
                else:
                    state = "ACTIVE ✅" if is_active(u) else "EXPIRED ❌"
                    tg_send(chat_id, f"👤 <b>VIP Status</b>\n\nStatus: {state}\nDays left: {days_left(u)} 🌙")
            return jsonify(ok=True)

        if txt == "Galleries":
            links = read_galleries()
            if not links:
                tg_send(chat_id, "⚠️ No galleries available yet 🔮")
                return jsonify(ok=True)

            free_gallery = links[0]
            ensure_schema_safe()
            with SessionLocal() as db:
                try:
                    u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
                except (ProgrammingError, OperationalError):
                    ensure_schema_safe()
                    u = None

                if not u or not is_active(u):
                    tg_send(chat_id, f"🖼️ <b>Free Gallery</b>\n{esc(free_gallery)} 🌹\n\nUnlock more with <b>VIP</b> 💋")
                    return jsonify(ok=True)

                today = day_mx()
                if u.last_sent_at and u.last_sent_at.date() == today:
                    tg_send(chat_id, "✨ You already received today’s muse 🌙")
                else:
                    vip_link = pick_vip_gallery(db, chat_id)
                    if not vip_link:
                        tg_send(chat_id, "⚠️ No VIP galleries available yet 🔮")
                    else:
                        tg_send(chat_id, f"🎁 <b>Your muse today</b>\n{esc(vip_link)} 💋")
                        record_delivery(db, chat_id, vip_link)
                        u.last_sent_at = now_mx().replace(tzinfo=None)
                        db.commit()
            return jsonify(ok=True)

        tg_send(chat_id, "✨ Choose an option below 💫")
        return jsonify(ok=True)

    except Exception as e:
        err = traceback.format_exc()
        notify_owner(f"🔥 Telegram handler crashed:\n<pre>{esc(err)}</pre>")
        return jsonify(ok=True)

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
