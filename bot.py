# bot.py
from flask import Flask, request, jsonify
import os
from datetime import datetime, timezone
from typing import Optional, List, Tuple
import requests

from sqlalchemy import create_engine, Column, Integer, String, BigInteger, Text
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Mapped, mapped_column

# ========= ENV VARS (no edites aquí; configúralas en Render) =========
TOKEN = os.environ["TOKEN"]                    # ej: 12345:ABC...
BASE_URL = os.environ["BASE_URL"]              # ej: https://puremusebot.onrender.com
MP_ACCESS_TOKEN = os.environ["MP_ACCESS_TOKEN"]# Token de Mercado Pago
DATABASE_URL = os.environ["DATABASE_URL"]      # postgresql+psycopg://.../db?sslmode=require
CRON_TOKEN = os.environ.get("CRON_TOKEN", "")  # para proteger /cron/daily
# =====================================================================

VIP_DURATION_SECONDS = 30 * 24 * 3600  # 30 días

SEND_URL     = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL   = "https://api.mercadopago.com/v1/payments/"

# ---------- SQLAlchemy ----------
class Base(DeclarativeBase):
    pass

class VipAccess(Base):
    __tablename__ = "vip_access"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    access_until: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_payment_id: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)

class Gallery(Base):
    __tablename__ = "galleries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text)
    active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

class VipProgress(Base):
    __tablename__ = "vip_progress"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_gallery_id: Mapped[Optional[int]] = mapped_column(Integer)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def init_db():
    Base.metadata.create_all(engine)

# ---------- Utils ----------
def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def tg_send(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(SEND_URL, json=payload, timeout=15)
    except Exception as e:
        app.logger.error(f"tg_send error: {e}")

def seconds_to_dhm(secs: int) -> Tuple[int, int, int]:
    d = secs // 86400
    h = (secs % 86400) // 3600
    m = (secs % 3600) // 60
    return d, h, m

# ---------- DB ops ----------
def db_upsert_vip(chat_id: int, access_until_epoch: int, payment_id: Optional[str]):
    with SessionLocal() as s:
        obj = s.get(VipAccess, chat_id)
        if obj:
            obj.access_until = access_until_epoch
            if payment_id:
                obj.last_payment_id = payment_id
            obj.status = "active"
        else:
            s.add(VipAccess(
                chat_id=chat_id,
                access_until=access_until_epoch,
                last_payment_id=payment_id or "",
                status="active",
            ))
        s.commit()

def db_get_vip(chat_id: int):
    with SessionLocal() as s:
        obj = s.get(VipAccess, chat_id)
        if not obj:
            return None
        return (obj.access_until, obj.status)

def db_set_progress(chat_id: int, last_gallery_id: int):
    with SessionLocal() as s:
        obj = s.get(VipProgress, chat_id)
        if obj:
            obj.last_gallery_id = last_gallery_id
        else:
            s.add(VipProgress(chat_id=chat_id, last_gallery_id=last_gallery_id))
        s.commit()

def db_get_progress(chat_id: int):
    with SessionLocal() as s:
        obj = s.get(VipProgress, chat_id)
        return obj.last_gallery_id if obj else None

def db_add_gallery(url: str, title: Optional[str]):
    with SessionLocal() as s:
        exists = s.query(Gallery).filter(Gallery.url == url).first()
        if exists:
            return
        s.add(Gallery(url=url, title=title or "", active=1, created_at=now_epoch()))
        s.commit()

def db_list_galleries():
    with SessionLocal() as s:
        return s.query(Gallery.id, Gallery.url, Gallery.title, Gallery.active)\
                .order_by(Gallery.id.asc()).all()

def db_next_gallery_for(chat_id: int):
    last_id = db_get_progress(chat_id)
    with SessionLocal() as s:
        q = s.query(Gallery.id, Gallery.url, Gallery.title).filter(Gallery.active == 1)
        if last_id is None:
            row = q.order_by(Gallery.id.asc()).first()
        else:
            row = q.filter(Gallery.id > last_id).order_by(Gallery.id.asc()).first()
        return (row.id, row.url, row.title) if row else None

# ---------- Sincronizar galleries.txt ----------
def sync_galleries_from_file(file_path="galleries.txt"):
    try:
        if not os.path.exists(file_path):
            app.logger.warning("galleries.txt no encontrado; continuando.")
            return
        added = 0
        with open(file_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) == 2:
                    title, url = parts[0].strip(), parts[1].strip()
                else:
                    title, url = "", parts[0].strip()
                if url.startswith("http"):
                    db_add_gallery(url, title)
                    added += 1
        app.logger.info(f"✅ Sincronizadas {added} entradas desde galleries.txt")
    except Exception as e:
        app.logger.error(f"sync_galleries_from_file error: {e}")

# ---------- Mercado Pago ----------
def mp_create_preference_for_user(chat_id: int, title="PureMuse VIP – 30 days", qty=1, unit_price=99.0, currency_id="MXN") -> str:
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
        "notification_url": f"{BASE_URL}/mp/webhook",
        "external_reference": str(chat_id)
    }
    r = requests.post(MP_PREFS_URL, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("init_point") or data.get("sandbox_init_point")

def mp_get_payment(payment_id: str):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(MP_PAY_URL + str(payment_id), headers=headers, timeout=30)
    return r.json() if r.status_code == 200 else None

# ---------- Textos ----------
WELCOME = "🌹 *Welcome to PureMuse.*\nWhere art meets sensuality.\n\nChoose one of the options below 👇"
ABOUT   = "*PureMuse* is a digital gallery where art and sensuality merge.\nExclusive photographic collections, elegant aesthetics, and the beauty of desire."
COLLECT = "🖼️ *PureMuse Collections*\n• Noir & Gold Edition\n• Veils & Silhouettes\n• Amber Light\n_(Demo)_"
HELP    = "Available commands:\n/start, /about, /collections, /pay, /content, /vip, /renew, /support, /help"
PAID_OK = "✅ Payment received. VIP access is *active for 30 days*.\nUse `/content` to get today’s gallery, or `/vip` to check your status."

# ---------- Flask ----------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "PureMuse Bot is up ✨ Stage 5 (Neon/Postgres)", 200

@app.route("/healthz", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

# Webhook Telegram
@app.route("/webhook", methods=["POST","GET"])
def webhook():
    if request.method == "GET":
        return "Webhook OK", 200
    data = request.get_json(silent=True) or {}
    msg  = data.get("message") or data.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip().lower()
    if not chat_id or not text:
        return "OK", 200

    if text.startswith("/start"):
        keyboard = {
            "keyboard": [
                [{"text": "/about"}, {"text": "/collections"}],
                [{"text": "/pay"}, {"text": "/content"}],
                [{"text": "/vip"}, {"text": "/support"}]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False
        }
        tg_send(chat_id, WELCOME, reply_markup=keyboard)

    elif text.startswith("/about"):
        tg_send(chat_id, ABOUT)

    elif text.startswith("/collections"):
        tg_send(chat_id, COLLECT)

    elif text.startswith("/help"):
        tg_send(chat_id, HELP)

    elif text.startswith("/vip"):
        rec = db_get_vip(chat_id)
        if not rec:
            tg_send(chat_id, "❌ No active VIP access. Use /pay or /renew.")
        else:
            access_until, status = rec
            remaining = access_until - now_epoch()
            if remaining > 0 and status == "active":
                d, h, m = seconds_to_dhm(remaining)
                tg_send(chat_id, f"✅ VIP active. Remaining: *{d}d {h}h {m}m*.")
            else:
                tg_send(chat_id, "⛔ VIP expired. Use /renew to reactivate.")

    elif text.startswith("/content"):
        rec = db_get_vip(chat_id)
        if not rec:
            tg_send(chat_id, "❌ No VIP access. Use /pay to subscribe.")
        else:
            access_until, status = rec
            if (access_until - now_epoch()) <= 0 or status != "active":
                tg_send(chat_id, "⛔ Access expired. Use /renew to continue.")
            else:
                nxt = db_next_gallery_for(chat_id)
                if not nxt:
                    tg_send(chat_id, "🎉 You already received all available galleries. Come back tomorrow!")
                else:
                    gid, url, title = nxt
                    title_txt = f"*{title}*\n" if title else ""
                    tg_send(chat_id, f"🍷 *Premium Gallery*\n{title_txt}{url}")
                    db_set_progress(chat_id, gid)

    elif text.startswith("/renew"):
        try:
            pay_url = mp_create_preference_for_user(chat_id)
            tg_send(chat_id, f"💳 *Renew VIP (30 days)*\n👉 [Pay now]({pay_url})")
        except Exception as e:
            tg_send(chat_id, f"⚠️ Payment error: {e}")

    elif text.startswith("/pay") or text.startswith("/buy"):
        try:
            pay_url = mp_create_preference_for_user(chat_id)
            tg_send(chat_id, f"💎 *VIP Access (30 days)*\nPrice: $99 MXN\n👉 [Pay now]({pay_url})")
        except Exception as e:
            tg_send(chat_id, f"⚠️ Error creating payment link: {e}")

    elif text.startswith("/support"):
        tg_send(chat_id, "✉️ Support: contact@puremuse.example\nReplies within 24–48 hours.")

    else:
        tg_send(chat_id, "Unknown command. Use /help to see options.")
    return "OK", 200

# Webhook Mercado Pago (activa VIP)
@app.route("/mp/webhook", methods=["POST","GET"])
def mp_webhook():
    if request.method == "GET":
        return "MP Webhook OK", 200
    body = request.get_json(silent=True) or {}
    topic = body.get("type") or body.get("topic")
    payment_id = (body.get("data") or {}).get("id")
    if topic == "payment" and payment_id:
        pay = mp_get_payment(payment_id)
        if pay:
            status  = pay.get("status")
            ext_ref = pay.get("external_reference")
            try:
                chat_id = int(ext_ref) if ext_ref else None
            except Exception:
                chat_id = None
            app.logger.info(f"[MP] Payment {payment_id} -> {status} (chat_id={chat_id})")
            if status == "approved" and chat_id:
                access_until = now_epoch() + VIP_DURATION_SECONDS
                db_upsert_vip(chat_id, access_until, str(payment_id))
                tg_send(chat_id, PAID_OK)
    return jsonify({"status": "received"}), 200

# Cron diario: envía la próxima galería a todos los VIP activos
@app.route("/cron/daily", methods=["GET","POST"])
def cron_daily():
    # Protegido con token ?key=...
    given = request.args.get("key", "")
    if CRON_TOKEN and given != CRON_TOKEN:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    now_e = now_epoch()
    sent = 0
    users: List[int] = []

    with SessionLocal() as s:
        rows = s.query(VipAccess.chat_id, VipAccess.access_until, VipAccess.status)\
                .filter(VipAccess.access_until > now_e, VipAccess.status == "active").all()
        users = [r[0] for r in rows]

    for chat_id in users:
        nxt = db_next_gallery_for(chat_id)
        if not nxt:
            continue
        gid, url, title = nxt
        title_txt = f"*{title}*\n" if title else ""
        tg_send(chat_id, f"🌙 *Daily VIP drop*\n{title_txt}{url}")
        db_set_progress(chat_id, gid)
        sent += 1

    return jsonify({"active_users": len(users), "sent": sent}), 200

# ---------- Bootstrap ----------
def startup():
    init_db()                # crea tablas si no existen
    sync_galleries_from_file()

startup()
