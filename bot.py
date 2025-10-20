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
    UniqueConstraint, text, delete, select, func
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

class VIPPayment(Base):
    __tablename__ = "vip_payments"
    __table_args__ = (UniqueConstraint("mp_payment_id", name="uq_payment_mp_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    mp_payment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_mxn: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="MXN")
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


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
    """Devuelve True si el usuario sigue activo."""
    try:
        now = now_mx().replace(tzinfo=None)  # ← Forzamos a naive
        if not u.active_until:
            return False
        return now < u.active_until
    except Exception:
        return False

def days_left(u: VIPUser) -> int:
    """Devuelve cuántos días le quedan al usuario VIP."""
    try:
        now = now_mx().replace(tzinfo=None)
        if not u.active_until:
            return 0
        delta = u.active_until - now
        return max(0, delta.days)
    except Exception:
        return 0

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
@app.post("/mp/webhook")
def mp_webhook():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403

    d = request.get_json(silent=True) or {}
    pid = d.get("data", {}).get("id") or d.get("id")
    if not pid:
        return "missing id", 400

    p = mp_fetch_payment(pid)
    if not p:
        return "not found", 404

    status = p.get("status")
    amount = p.get("transaction_amount")
    currency = p.get("currency_id")
    chat_id = int(p.get("external_reference") or 0)

    if status == "approved" and amount == 50 and currency == "MXN" and chat_id:
        ensure_schema_safe()
        with SessionLocal() as db:
            now = now_mx()
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if u:
                u.start_date = now.date()
                u.active_until = (now + timedelta(days=30)).replace(tzinfo=None)
                u.last_sent_at = None
            else:
                db.add(VIPUser(
                    chat_id=chat_id,
                    username=None,
                    start_date=now.date(),
                    active_until=(now + timedelta(days=30)).replace(tzinfo=None),
                    last_sent_at=None
                ))
            db.commit()

        tg_send(chat_id, "💋 <b>Payment approved!</b>\n\nYour VIP is now active for <b>30 days</b> ✨")
        notify_owner(f"💳 New VIP payment from user <b>{chat_id}</b> ✅")

        # 📦 Registrar el pago para métricas
        try:
            ensure_schema_safe()
            with SessionLocal() as db:
                existing = db.execute(
                    select(VIPPayment).where(VIPPayment.mp_payment_id == str(pid))
                ).scalar_one_or_none()
                if not existing:
                    db.add(VIPPayment(
                        chat_id=chat_id,
                        mp_payment_id=str(pid),
                        amount_mxn=int(amount or 0),
                        currency=currency or "MXN",
                        status=status,
                        approved_at=now_mx().replace(tzinfo=None),
                    ))
                    db.commit()
        except Exception as e:
            notify_owner(f"⚠️ Could not record payment {pid}: <pre>{esc(str(e))}</pre>")

    return "ok", 200


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
        
# ======= ADMIN ENDPOINTS (safe) =======
@app.get("/admin/delete_user")
def admin_delete_user():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    chat_id = request.args.get("chat_id", type=int)
    if not chat_id:
        return jsonify(ok=False, error="missing chat_id"), 400

    ensure_schema_safe()
    try:
        with SessionLocal() as db:
            # Borrar entregas primero (si existen)
            d1 = db.execute(delete(VIPDelivery).where(VIPDelivery.chat_id == chat_id)).rowcount
            d2 = db.execute(delete(VIPUser).where(VIPUser.chat_id == chat_id)).rowcount
            db.commit()
        return jsonify(ok=True, deleted=(d1 or 0) + (d2 or 0)), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500


@app.get("/admin/clear_all")
def admin_clear_all():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    ensure_schema_safe()
    try:
        with SessionLocal() as db:
            d1 = db.execute(delete(VIPDelivery)).rowcount
            d2 = db.execute(delete(VIPUser)).rowcount
            db.commit()
        return jsonify(ok=True, cleared_users=(d2 or 0), cleared_deliveries=(d1 or 0)), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500


@app.get("/admin/db_status")
def admin_db_status():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    ensure_schema_safe()
    try:
        with SessionLocal() as db:
            users = db.execute(select(func.count(VIPUser.id))).scalar_one()
            deliveries = db.execute(select(func.count(VIPDelivery.id))).scalar_one()
            last_backup = db.execute(select(func.max(VIPDelivery.sent_at))).scalar_one_or_none()
        return jsonify(ok=True, users=users, deliveries=deliveries, last_backup=str(last_backup)), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500

@app.get("/admin/metrics/overview")
def metrics_overview():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    ensure_schema_safe()
    try:
        today = day_mx()
        last_30 = today - timedelta(days=30)

        with SessionLocal() as db:
            all_users = list(db.execute(select(VIPUser)).scalars())
            users_total = len(all_users)
            users_active = sum(1 for u in all_users if is_active(u))
            users_expired = users_total - users_active

            deliveries_total = db.execute(select(func.count(VIPDelivery.id))).scalar_one() or 0
            deliveries_today = db.execute(
                select(func.count(VIPDelivery.id)).where(func.date(VIPDelivery.sent_at) == today)
            ).scalar_one() or 0
            last_delivery_at = db.execute(select(func.max(VIPDelivery.sent_at))).scalar_one_or_none()

            payments_total_count = db.execute(select(func.count(VIPPayment.id))).scalar_one() or 0
            revenue_mxn_total = db.execute(select(func.coalesce(func.sum(VIPPayment.amount_mxn), 0))).scalar_one() or 0
            revenue_mxn_30d = db.execute(
                select(func.coalesce(func.sum(VIPPayment.amount_mxn), 0)).where(
                    func.date(VIPPayment.approved_at) >= last_30
                )
            ).scalar_one() or 0

            new_vip_today = db.execute(
                select(func.count(VIPPayment.id)).where(func.date(VIPPayment.approved_at) == today)
            ).scalar_one() or 0
            expiring_today = sum(1 for u in all_users if u.active_until and u.active_until.date() == today)

        return jsonify(
            ok=True,
            users_total=users_total,
            users_active=users_active,
            users_expired=users_expired,
            deliveries_total=deliveries_total,
            deliveries_today=deliveries_today,
            last_delivery_at=str(last_delivery_at),
            payments_total_count=payments_total_count,
            revenue_mxn_total=revenue_mxn_total,
            revenue_mxn_30d=revenue_mxn_30d,
            new_vip_today=new_vip_today,
            expiring_today=expiring_today,
        ), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500

@app.get("/admin/metrics/revenue_by_day")
def metrics_revenue_by_day():
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    ensure_schema_safe()
    try:
        today = day_mx()
        start = today - timedelta(days=13)

        rows = []
        with SessionLocal() as db:
            q = db.execute(
                select(
                    func.date(VIPPayment.approved_at).label("d"),
                    func.coalesce(func.sum(VIPPayment.amount_mxn), 0).label("sum_mxn")
                ).where(
                    func.date(VIPPayment.approved_at) >= start
                ).group_by(
                    func.date(VIPPayment.approved_at)
                ).order_by("d")
            ).all()

            map_by_day = {str(r.d): int(r.sum_mxn) for r in q if r.d}
            cur = start
            while cur <= today:
                rows.append({"date": str(cur), "revenue_mxn": map_by_day.get(str(cur), 0)})
                cur += timedelta(days=1)

        return jsonify(ok=True, days=rows), 200
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500

# ========= MAIN =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)


