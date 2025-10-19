# =========================================================
# PureMuse Telegram Bot – Version 3.0 (Self-Healing Edition)
# Autor: Luhit + ChatGPT
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
    create_engine, BigInteger, Integer, String, Date, DateTime, JSON, select, UniqueConstraint, func, text
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column

# ========= ENV VARS (Render) =========
TOKEN = os.getenv("TOKEN", "")
BASE_URL = (os.getenv("BASE_URL", "") or "").rstrip("/")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
CRON_TOKEN = os.getenv("CRON_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
OWNER_CHAT_ID = 1703988973  # tu chat_id personal

# ========= Telegram / MP URLs =========
TG_BASE = f"https://api.telegram.org/bot{TOKEN}"
TG_SEND_URL = f"{TG_BASE}/sendMessage"
TG_SET_WEBHOOK_URL = f"{TG_BASE}/setWebhook"
TG_GET_WEBHOOK_INFO_URL = f"{TG_BASE}/getWebhookInfo"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL = "https://api.mercadopago.com/v1/payments/"
TZ_MX = ZoneInfo("America/Mexico_City")

# ========= DATABASE CONFIG =========
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

# ========= MODELOS =========
class Base(DeclarativeBase):
    pass

class VIPUser(Base):
    __tablename__ = "vip_users"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_vip_chat_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(150))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    active_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class VIPDelivery(Base):
    __tablename__ = "vip_deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    gallery_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class VIPBackup(Base):
    __tablename__ = "vip_backups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    backed_up_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

# ========= AUTO-SCHEMA (corrección automática) =========
def ensure_schema():
    """Crea tablas y columnas faltantes sin borrar datos."""
    with engine.begin() as conn:
        # Crear tablas si no existen
        Base.metadata.create_all(bind=engine)

        # Asegurar columnas críticas en vip_users
        cols = {r[0] for r in conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='vip_users'")
        )}
        if "active_until" not in cols:
            conn.exec_driver_sql("ALTER TABLE vip_users ADD COLUMN active_until TIMESTAMP;")
        if "last_sent_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE vip_users ADD COLUMN last_sent_at TIMESTAMP;")
        if "updated_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE vip_users ADD COLUMN updated_at TIMESTAMP DEFAULT NOW();")
        print("✅ Database schema verified / updated.")

# Ejecutar verificación al iniciar
ensure_schema()

# ========= FUNCIONES UTILITARIAS =========
def esc(s: str) -> str: return html.escape(s, quote=True)
def now_mx() -> datetime: return datetime.now(tz=TZ_MX)
def day_mx() -> date: return now_mx().date()
def url_hash(u: str) -> str: return hashlib.sha256(u.encode()).hexdigest()

def is_active(u: VIPUser) -> bool:
    return now_mx() < u.active_until

def days_left(u: VIPUser) -> int:
    return max(0, (u.active_until - now_mx()).days)

def read_galleries() -> list[str]:
    path = os.path.join(os.getcwd(), "galleries.txt")
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip()]

def build_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "Pure Muse"}, {"text": "VIP"}],
            [{"text": "Galleries"}, {"text": "VIP status"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }

def tg_send(chat_id: int, text: str, preview=False, kb=True):
    payload = {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": not preview
    }
    if kb: payload["reply_markup"] = build_keyboard()
    try:
        requests.post(TG_SEND_URL, json=payload, timeout=15)
    except Exception as e:
        print("⚠️ Telegram send exception:", e)

def notify_owner(msg: str):
    tg_send(OWNER_CHAT_ID, f"📣 {msg}", kb=False)

# ========= RESPALDOS =========
def backup_user(u: VIPUser):
    """Guarda una copia JSON del estado del usuario."""
    data = {
        "chat_id": u.chat_id,
        "username": u.username,
        "start_date": str(u.start_date),
        "active_until": str(u.active_until),
        "last_sent_at": str(u.last_sent_at)
    }
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO vip_backups (chat_id, data) VALUES (:cid, :data)"),
            {"cid": u.chat_id, "data": data}
        )

# ========= FLASK =========
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify(ok=True, service="PureMuse Bot v3.0"), 200

@app.get("/admin/db_status")
def db_status():
    """Muestra conteo de usuarios, envíos y último backup."""
    if request.args.get("secret") != CRON_TOKEN:
        return "forbidden", 403
    ensure_schema()
    with SessionLocal() as db:
        users = db.execute(select(func.count(VIPUser.id))).scalar_one()
        deliveries = db.execute(select(func.count(VIPDelivery.id))).scalar_one()
        last_backup = db.execute(select(func.max(VIPBackup.backed_up_at))).scalar_one()
    return jsonify(ok=True, users=users, deliveries=deliveries, last_backup=str(last_backup)), 200

# ... (aquí sigue tu lógica de Telegram, VIP, Galleries, MP, etc. igual que antes)
# No se altera nada funcional del bot.
