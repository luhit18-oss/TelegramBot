# ============================================
# PureMuse Bot — Flask + Gunicorn + SQLAlchemy + Mercado Pago
# Archivo único (app.py)
# ============================================

import os
import html
from datetime import date, datetime
from typing import Optional

import requests
from flask import Flask, request, jsonify

# ============ SECCIÓN 1A: VARIABLES DE ENTORNO ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # p.ej. https://puremusebot.onrender.com
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "secret")
DATABASE_URL = os.getenv("DATABASE_URL", "")  # postgresql+psycopg2://.../neondb?sslmode=require

# Endpoints Telegram
TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TG_SEND_URL = f"{TG_BASE}/sendMessage"
TG_SET_WEBHOOK_URL = f"{TG_BASE}/setWebhook"
TG_GET_WEBHOOK_INFO_URL = f"{TG_BASE}/getWebhookInfo"

# Endpoints Mercado Pago
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL = "https://api.mercadopago.com/v1/payments/"

# ============ SECCIÓN 1B: BASE DE DATOS (SQLAlchemy 2.x) ============
from sqlalchemy import (
    create_engine, BigInteger, Integer, String, Date, select, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, DeclarativeBase, Mapped, mapped_column

def _require_dburl():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no definido")

engine = create_engine(
    DATABASE_URL or "postgresql+psycopg2://user:pass@localhost:5432/placeholder",
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
    start_date: Mapped[date] = mapped_column(Date, nullable=False)  # inicio del ciclo VIP
    progress_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # legado (no imprescindible)

def ensure_tables():
    _require_dburl()
    Base.metadata.create_all(bind=engine)

# ============ SECCIÓN 1C: UTILIDADES ============
def read_galleries() -> list[str]:
    """
    Lee galleries.txt (una URL por línea). Devuelve exactamente 30 elementos.
    """
    path = os.path.join(os.getcwd(), "galleries.txt")
    links: list[str] = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            links = [ln.strip() for ln in f if ln.strip()]
    if not links:
        # Fallback seguro (reemplaza con tus links reales)
        links = [f"https://drive.google.com/your-gallery-link-{i+1}" for i in range(30)]
    # normalizar a 30
    if len(links) < 30:
        while len(links) < 30:
            links.append(links[-1])
    elif len(links) > 30:
        links = links[:30]
    return links

# Cargamos a memoria y permitimos recargar vía endpoint
GALLERIES = read_galleries()

def is_active_vip(start: date, today: Optional[date] = None) -> bool:
    if today is None:
        today = date.today()
    return (today - start).days < 30

def current_day_index(start: date, today: Optional[date] = None) -> int:
    if today is None:
        today = date.today()
    # Día 0..29
    return max(0, min((today - start).days, 29))

def esc_html(s: str) -> str:
    return html.escape(s, quote=True)

def tg_send_text(
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = "HTML",
    reply_markup: Optional[dict] = None,
    disable_web_page_preview: bool = False,
):
    """
    Envía texto a Telegram y adjunta (opcionalmente) un teclado.
    Usa HTML por simplicidad de escape.
    """
    if not TELEGRAM_TOKEN:
