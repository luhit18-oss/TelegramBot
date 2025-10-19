# =========================================================
# PureMuse Telegram Bot – Version 2.1 (Erotic Elegant Edition)
# Vars (Render): TOKEN, BASE_URL, MP_ACCESS_TOKEN, CRON_TOKEN, DATABASE_URL
# Owner notifications to chat_id below.
# Features:
#   🌹 Erotic & Elegant style + emojis
#   🔔 Owner notifications (payments, daily run, expirations)
#   🕛 Auto-expiration message (30 days)
#   📊 /stats endpoint (protected)
#   🎁 Daily VIP galleries (manual trigger via /cron/daily)
#   🆓 "Galleries" for non-VIP returns line 1 of galleries.txt (free demo)
#   💎 VIP galleries are picked ONLY from line 2 onwards (no repeats per user)
# =========================================================

import os
import html
import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Iterable

import requests
from flask import Flask, request, jsonify

from sqlalchemy import (
    create_engine, BigInteger, Integer, String, Date, DateTime, select, UniqueConstraint, func
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column

# ========= ENV VARS (Render) =========
TOKEN = os.getenv("TOKEN", "")
BASE_URL = (os.getenv("BASE_URL", "") or "").rstrip("/")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
OWNER_CHAT_ID = 1703988973  # <— tu chat_id (notificaciones al dueño)

# ========= Constantes externas =========
TG_BASE = f"https://api.telegram.org/bot{TOKEN}"
TG_SEND_URL = f"{TG_BASE}/sendMessage"
TG_SET_WEBHOOK_URL = f"{TG_BASE}/setWebhook"
TG_GET_WEBHOOK_INFO_URL = f"{TG_BASE}/getWebhookInfo"

MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL = "https://api.mercadopago.com/v1/payments/"

TZ_MX = ZoneInfo("America/Mexico_City")

# ========= DATABASE =========
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
        "one_time_keyboard": False,
        "is_persistent": True,
        "input_field_placeholder": "Choose an option…",
    }

def tg_send(chat_id: int, text: str, preview=False, kb=True):
    if not TOKEN:
        print("⚠️ TOKEN vacío; no se envió mensaje")
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
        r = requests.post(TG_SEND_URL, json=payload, timeout=15)
        if r.status_code != 200:
            print("⚠️ Telegram error:", r.status_code, r.text)
    except Exception as e:
        print("⚠️ Telegram send exception:", e)

def notify_owner(text: str):
    try:
        tg_send(OWNER_CHAT_ID, text, preview=False, kb=False)
    except Exception as e:
        print("⚠️ Owner notify error:", e)

# ========= Mercado Pago =========
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
    print("⚠️ MP fetch error:", r.status_code, r.text)
    return None

# ========= Galleries picking (VIP only from line 2+) =========
def pick_new_from_pool(db, chat_id: int, pool: Iterable[str]) -> Optional[str]:
    """Returns first URL from pool the user has NEVER received."""
    sent_hashes = {
        row[0] for row in db.execute(select(VIPDelivery.gallery_hash).where(VIPDelivery.chat_id == chat_id))
    }
    for link in pool:
        if url_hash(link) not in sent_hashes:
            return link
    return None

def pick_vip_gallery(db, chat_id: int) -> Optional[str]:
    links = read_galleries()
    if len(links) <= 1:
        return None  # no VIP pool available
    vip_pool = links[1:]  # only from line 2 onwards
    return pick_new_from_pool(db, chat_id, vip_pool)

def record_delivery(db, chat_id: int, url: str):
    db.add(VIPDelivery(
        chat_id=chat_id,
        gallery_hash=url_hash(url),
        url=url,
        sent_at=now_mx().replace(tzinfo=None),
    ))

# ========= Flask =========
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify(ok=True, service="PureMuse Bot v2.1"), 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/testdb")
def testdb():
    ensure_tables()
    return "✅ DB ready", 200

@app.get("/set_webhook")
def set_webhook():
    if not BASE_URL or not TOKEN:
        return jsonify(ok=False, error="Set BASE_URL and TOKEN"), 400
    url = f"{BASE_URL}/telegram"
    r = requests.get(TG_SET_WEBHOOK_URL, params={"url": url}, timeout=15)
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify(ok=False, raw=r.text), r.status_code

@app.get("/get_webhook_info")
def get_webhook_info():
    if not TOKEN:
        return jsonify(ok=False, error="Set TOKEN"), 400
    r = requests.get(TG_GET_WEBHOOK_INFO_URL, timeout=15)
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify(ok=False, raw=r.text), r.status_code

@app.get("/paid")
def paid():
    return f"Payment status: {request.args.get('status','unknown')}", 200

# ======= MERCADO PAGO WEBHOOK =======
@app.post("/mp/webhook")
def mp_webhook():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    p = request.get_json(silent=True) or {}
    pid = (p.get("data") or {}).get("id") or p.get("id") or (p.get("resource", "").split("/")[-1] if p.get("resource") else None)
    if not pid:
        return "ok", 200
    pay = mp_fetch_payment(str(pid))
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

    if status == "approved" and amount == 50 and currency == "MXN" and chat_id:
        ensure_tables()
        with SessionLocal() as db:
            now = now_mx()
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if u:
                u.start_date = now.date()
                u.active_until = (now + timedelta(days=30)).replace(tzinfo=None)
                u.last_sent_at = None
            else:
                u = VIPUser(
                    chat_id=chat_id,
                    username=None,
                    start_date=now.date(),
                    active_until=(now + timedelta(days=30)).replace(tzinfo=None),
                    last_sent_at=None,
                )
                db.add(u)
            db.commit()
        tg_send(chat_id, "💋 <b>Payment approved!</b>\n\nYour VIP is now alive for <b>30 days</b> of beauty & desire ✨")
        notify_owner(f"💳 New VIP payment from user <b>{chat_id}</b> ✅")
    return "ok", 200

# ======= TELEGRAM WEBHOOK =======
@app.post("/telegram")
def telegram_webhook():
    d = request.get_json(silent=True) or {}
    msg = d.get("message") or d.get("edited_message")
    if not msg:
        return jsonify(ok=True)
    chat_id = msg["chat"]["id"]
    txt = (msg.get("text") or "").strip()

    # /start or "menu"
    if txt.lower() in ("/start", "menu"):
        # Welcome mentions free gallery
        tg_send(
            chat_id,
            "✨ <b>Welcome to Pure Muse</b>\n\n"
            "Where art meets desire. Try your <b>first gallery for free</b> via <b>Galleries</b> 🌹\n"
            "Unlock <b>30 days</b> of daily private links with <b>VIP</b> 💋"
        )
        return jsonify(ok=True)

    # Pure Muse
    if txt == "Pure Muse":
        tg_send(
            chat_id,
            "🌹 <b>Pure Muse</b>\n\nArtistic sensuality. Every night, a new secret unveiled. "
            "Use <b>VIP</b> to awaken your muse for 30 days 🔥"
        )
        return jsonify(ok=True)

    # VIP -> create payment link
    if txt == "VIP":
        try:
            link = mp_create_link(chat_id)
            tg_send(chat_id, f"💳 <b>VIP Access</b>\n\n<b>$50 MXN</b> for <b>30 days</b>.\n"
                             f"Complete your payment here:\n{esc(link)} 💋", preview=False)
        except Exception as e:
            tg_send(chat_id, f"⚠️ Could not create payment link.\n{esc(str(e))}")
        return jsonify(ok=True)

    # VIP status
    if txt == "VIP status":
        ensure_tables()
        with SessionLocal() as db:
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if not u:
                tg_send(chat_id, "❌ No VIP found. Tap <b>VIP</b> to begin your affair ✨")
            else:
                state = "ACTIVE ✅" if is_active(u) else "EXPIRED ❌"
                tg_send(chat_id, f"👤 <b>VIP Status</b>\n\nStatus: {state}\nDays left: {days_left(u)} 🌙")
        return jsonify(ok=True)

    # Galleries:
    # - Non-VIP: send FREE gallery => first line of galleries.txt
    # - VIP: send an exclusive gallery from line 2 onwards (no repeats)
    if txt == "Galleries":
        links = read_galleries()
        if not links:
            tg_send(chat_id, "⚠️ No galleries available yet 🔮")
            return jsonify(ok=True)

        free_gallery = links[0]  # public demo
        ensure_tables()
        with SessionLocal() as db:
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()

            # Non-VIP -> always show free gallery
            if not u or not is_active(u):
                tg_send(
                    chat_id,
                    f"🖼️ <b>Free Gallery</b>\n{esc(free_gallery)} 🌹\n\n"
                    f"Unlock <b>30 more nights</b> with <b>VIP</b> 💋",
                    preview=False
                )
                return jsonify(ok=True)

            # VIP active -> send daily exclusive (from line 2+)
            today = day_mx()
            if u.last_sent_at and u.last_sent_at.date() == today:
                tg_send(chat_id, "✨ You already received today’s muse. Come back tomorrow 🌙")
            else:
                vip_link = pick_vip_gallery(db, chat_id)
                if not vip_link:
                    tg_send(chat_id, "⚠️ No VIP galleries available yet 🔮")
                else:
                    tg_send(chat_id, f"🎁 <b>Your muse today</b>\n{esc(vip_link)} 💋", preview=False)
                    record_delivery(db, chat_id, vip_link)
                    u.last_sent_at = now_mx().replace(tzinfo=None)
                    db.commit()
        return jsonify(ok=True)

    # Fallback
    tg_send(chat_id, "✨ Choose an option below 💫")
    return jsonify(ok=True)

# ======= CRON DAILY (manual trigger) =======
@app.post("/cron/daily")
def cron_daily():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    ensure_tables()
    now = now_mx()
    sent = 0
    expired_count = 0
    with SessionLocal() as db:
        users = list(db.execute(select(VIPUser)).scalars())
        for u in users:
            # Expiration same day -> notify once (when it just flipped)
            if not is_active(u) and (now.date() - u.active_until.date()).days == 0:
                tg_send(u.chat_id, "🌙 <b>Your VIP fades tonight…</b>\nRenew to awaken your muse again 💋")
                notify_owner(f"⚠️ VIP expired for user {u.chat_id}")
                expired_count += 1
                continue
            # Active users: send if not sent today
            if is_active(u):
                if not u.last_sent_at or u.last_sent_at.date() < now.date():
                    link = pick_vip_gallery(db, u.chat_id)
                    if link:
                        tg_send(u.chat_id, f"🎁 <b>Your muse awaits…</b>\n{esc(link)} 💋", preview=False)
                        record_delivery(db, u.chat_id, link)
                        u.last_sent_at = now.replace(tzinfo=None)
                        db.commit()
                        sent += 1
        notify_owner(f"🕛 Daily delivery complete.\nSent: {sent} ✨\nExpired notices: {expired_count}")
    return jsonify(ok=True, sent=sent, expired=expired_count), 200

# ======= STATS =======
@app.get("/stats")
def stats():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    ensure_tables()
    today = day_mx()
    with SessionLocal() as db:
        all_users = list(db.execute(select(VIPUser)).scalars())
        active = sum(1 for u in all_users if is_active(u))
        expired = len(all_users) - active
        deliveries_today = db.execute(
            select(func.count(VIPDelivery.id)).where(func.date(VIPDelivery.sent_at) == today)
        ).scalar_one()
    return jsonify(ok=True, active=active, expired=expired, deliveries_today=deliveries_today), 200

# ======= MAIN =======
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
