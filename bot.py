# =========================================================
# PureMuse Telegram Bot – Version 2.0 (Erotic Elegant Edition)
# Author: Luhit & ChatGPT 🤍
# Features:
#   🌹 Erotic & Elegant style messages with emojis
#   🔔 Notifications to owner (ID 1703988973)
#   🕛 Auto-expiration message after 30 days
#   📊 /stats endpoint (protected)
#   🎁 Daily gallery system (manual or automatic trigger)
# =========================================================

import os
import html
import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import requests
from flask import Flask, request, jsonify
from sqlalchemy import (
    create_engine, BigInteger, Integer, String, Date, DateTime, select, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column

# ========= ENV VARS (Render) =========
TOKEN = os.getenv("TOKEN", "")
BASE_URL = (os.getenv("BASE_URL", "") or "").rstrip("/")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
OWNER_CHAT_ID = 1703988973  # tu chat ID

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

def ensure_tables():
    Base.metadata.create_all(bind=engine)

# ========= UTILS =========
def esc(s: str) -> str:
    return html.escape(s, quote=True)

def now_mx() -> datetime:
    return datetime.now(tz=TZ_MX)

def url_hash(u: str) -> str:
    return hashlib.sha256(u.encode()).hexdigest()

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
        "one_time_keyboard": False,
    }

def tg_send(chat_id: int, text: str, preview=False, kb=True):
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
        print("⚠️ Telegram send error:", e)

def mp_create_link(chat_id: int) -> str:
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
    return r.json().get("init_point")

# ========= CORE =========
def pick_new_gallery(db, chat_id: int) -> Optional[str]:
    links = read_galleries()
    if not links:
        return None
    sent = {row[0] for row in db.execute(select(VIPDelivery.gallery_hash).where(VIPDelivery.chat_id == chat_id))}
    for link in links:
        if url_hash(link) not in sent:
            return link
    return None

def record_delivery(db, chat_id: int, url: str):
    db.add(VIPDelivery(chat_id=chat_id, gallery_hash=url_hash(url), url=url, sent_at=now_mx().replace(tzinfo=None)))

# ========= FLASK =========
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify(ok=True, service="PureMuse Bot v2"), 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/testdb")
def testdb():
    ensure_tables()
    return "✅ DB ready", 200

# ======= MERCADO PAGO WEBHOOK =======
@app.post("/mp/webhook")
def mp_webhook():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    p = request.get_json(silent=True) or {}
    pid = (p.get("data") or {}).get("id") or p.get("id") or p.get("resource", "").split("/")[-1]
    if not pid: return "ok", 200
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(MP_PAY_URL + str(pid), headers=headers, timeout=20)
    if r.status_code != 200: return "ok", 200
    pay = r.json()
    if pay.get("status") == "approved" and pay.get("transaction_amount") == 50 and pay.get("currency_id") == "MXN":
        chat_id = int(pay.get("external_reference", 0))
        with SessionLocal() as db:
            ensure_tables()
            now = now_mx()
            user = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if user:
                user.start_date = now.date()
                user.active_until = now.replace(tzinfo=None) + timedelta(days=30)
                user.last_sent_at = None
            else:
                user = VIPUser(chat_id=chat_id, username=None, start_date=now.date(),
                               active_until=now.replace(tzinfo=None) + timedelta(days=30))
                db.add(user)
            db.commit()
        tg_send(chat_id, "💋 <b>Payment approved!</b>\n\nYour VIP is now active for 30 days of passion and art ✨")
        tg_send(OWNER_CHAT_ID, f"💳 New VIP payment from user <b>{chat_id}</b> ✅")
    return "ok", 200

# ======= TELEGRAM WEBHOOK =======
@app.post("/telegram")
def telegram_webhook():
    d = request.get_json(silent=True) or {}
    msg = d.get("message") or d.get("edited_message")
    if not msg: return jsonify(ok=True)
    chat_id = msg["chat"]["id"]
    txt = (msg.get("text") or "").strip()

    if txt.lower() in ("/start", "menu"):
        tg_send(chat_id, "✨ <b>Welcome to Pure Muse</b>\n\nWhere art meets desire. Use the buttons below 💋")
        return jsonify(ok=True)

    if txt == "Pure Muse":
        tg_send(chat_id, "🌹 <b>Pure Muse</b>\n\nArtistic sensuality, exclusive galleries. Unlock 30 days of beauty with VIP 🔥")
        return jsonify(ok=True)

    if txt == "VIP":
        try:
            link = mp_create_link(chat_id)
            tg_send(chat_id, f"💳 <b>VIP Access</b>\n\n$50 MXN for 30 days.\nComplete your payment here:\n{esc(link)} 💋", preview=False)
        except Exception as e:
            tg_send(chat_id, f"⚠️ Could not create payment link.\n{esc(str(e))}")
        return jsonify(ok=True)

    if txt == "VIP status":
        with SessionLocal() as db:
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if not u:
                tg_send(chat_id, "❌ No VIP found. Tap <b>VIP</b> to join ✨")
            else:
                state = "ACTIVE ✅" if is_active(u) else "EXPIRED ❌"
                tg_send(chat_id, f"👤 <b>VIP Status</b>\n\nStatus: {state}\nDays left: {days_left(u)} 🌙")
        return jsonify(ok=True)

    if txt == "Galleries":
        with SessionLocal() as db:
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if not u or not is_active(u):
                tg_send(chat_id, "🔒 Your muse sleeps until you renew VIP 💋")
            else:
                today = now_mx().date()
                if u.last_sent_at and u.last_sent_at.date() == today:
                    tg_send(chat_id, "✨ You already received today’s muse. Come back tomorrow 🌙")
                else:
                    link = pick_new_gallery(db, chat_id)
                    if not link:
                        tg_send(chat_id, "⚠️ No new galleries yet. Please wait 🔮")
                    else:
                        tg_send(chat_id, f"🎁 <b>Your muse today</b>:\n{esc(link)} 💋", preview=False)
                        record_delivery(db, chat_id, link)
                        u.last_sent_at = now_mx().replace(tzinfo=None)
                        db.commit()
        return jsonify(ok=True)

    tg_send(chat_id, "✨ Choose an option below 💫")
    return jsonify(ok=True)

# ======= CRON DAILY =======
@app.post("/cron/daily")
def cron_daily():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    now = now_mx()
    ensure_tables()
    sent, expired = 0, 0
    with SessionLocal() as db:
        users = list(db.execute(select(VIPUser)).scalars())
        for u in users:
            # Expired notification
            if not is_active(u) and (now.date() - u.active_until.date()).days == 0:
                tg_send(u.chat_id, "🌙 <b>Your VIP fades tonight...</b>\nRenew to awaken your muse again 💋")
                tg_send(OWNER_CHAT_ID, f"⚠️ VIP expired for user {u.chat_id}")
                expired += 1
                continue
            # Active users get galleries
            if is_active(u):
                if not u.last_sent_at or u.last_sent_at.date() < now.date():
                    link = pick_new_gallery(db, u.chat_id)
                    if link:
                        tg_send(u.chat_id, f"🎁 <b>Your muse awaits...</b>\n{esc(link)} 💋", preview=False)
                        record_delivery(db, u.chat_id, link)
                        u.last_sent_at = now.replace(tzinfo=None)
                        db.commit()
                        sent += 1
        tg_send(OWNER_CHAT_ID, f"🕛 Daily delivery complete.\nSent: {sent} ✨\nExpired: {expired}")
    return jsonify(ok=True, sent=sent, expired=expired), 200

# ======= STATS =======
@app.get("/stats")
def stats():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    with SessionLocal() as db:
        total = db.execute(select(VIPUser)).scalars().all()
        active = [u for u in total if is_active(u)]
        expired = [u for u in total if not is_active(u)]
        sent_today = db.query(VIPDelivery).count()
    return jsonify(ok=True, active=len(active), expired=len(expired), deliveries=sent_today), 200

# ======= MAIN =======
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
