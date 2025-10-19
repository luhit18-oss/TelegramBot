# ============================================
# PureMuse Bot — Bot de Telegram con menú + VIP
# Flask + Gunicorn + SQLAlchemy + psycopg2 + Mercado Pago
# ============================================

import os
from datetime import date
from flask import Flask, request, jsonify
import requests

# ============ SECCIÓN 1A: VARIABLES DE ENTORNO ============
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")  # p.ej. https://puremusebot.onrender.com
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "secret")
DATABASE_URL = os.getenv("DATABASE_URL", "")  # postgresql+psycopg2://.../neondb?sslmode=require

TG_SEND_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
TG_SET_WEBHOOK_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"

MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL = "https://api.mercadopago.com/v1/payments/"

# ============ SECCIÓN 1B: BASE DE DATOS (SQLAlchemy 2.x) ============
from sqlalchemy import create_engine, BigInteger, Integer, String, Date, select, UniqueConstraint
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
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)           # inicio del ciclo VIP
    progress_day: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0..29

def ensure_tables():
    Base.metadata.create_all(bind=engine)

# ============ SECCIÓN 1C: UTILIDADES ============
def read_galleries() -> list[str]:
    """
    Lee galleries.txt (una URL por línea).
    Devuelve exactamente 30 elementos (rellenando o recortando).
    """
    path = os.path.join(os.getcwd(), "galleries.txt")
    links = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            links = [ln.strip() for ln in f if ln.strip()]
    if not links:
        # Fallback seguro (reemplázalo por tus links reales)
        links = [f"https://drive.google.com/your-gallery-link-{i+1}" for i in range(30)]
    # normalizar a 30
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

def tg_send_text(chat_id: int, text: str, parse_mode: str | None = None, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(TG_SEND_URL, json=payload, timeout=15)

def build_main_menu() -> dict:
    """
    Menú principal como ReplyKeyboard (cada botón es un comando visible).
    """
    keyboard = [
        [{"text": "ABOUT"}, {"text": "GALLERIES"}],
        [{"text": "BUY VIP"}, {"text": "VIP STATUS"}],
    ]
    return {"keyboard": keyboard, "resize_keyboard": True, "one_time_keyboard": False}

def mp_create_link(chat_id: int) -> str:
    """
    Genera link de pago de Mercado Pago por $50 MXN.
    Guarda chat_id en external_reference para activación en webhook.
    """
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    data = {
        "items": [{"title": "PureMuse VIP – 30 días", "quantity": 1, "unit_price": 50.0, "currency_id": "MXN"}],
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

def send_gallery_today(chat_id: int, progress_day: int):
    day = max(0, min(progress_day, 29))
    link = GALLERIES[day]
    msg = f"🎁 *PureMuse VIP – Día {day+1}/30*\n\nTu galería de hoy:\n{link}\n\n¡Disfrútala!"
    tg_send_text(chat_id, msg, parse_mode="Markdown")

# ============ SECCIÓN 1D: FLASK APP Y RUTAS ============
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify({"ok": True, "service": "PureMuse Bot"}), 200

@app.get("/health")
def health():
    return "ok", 200

@app.get("/testdb")
def testdb():
    ensure_tables()
    return "✅ DB lista (tablas creadas/verificadas).", 200

@app.get("/set_webhook")
def set_webhook():
    if not BASE_URL or not TELEGRAM_TOKEN:
        return jsonify({"ok": False, "error": "Set BASE_URL and TELEGRAM_TOKEN"}), 400
    url = f"{BASE_URL}/telegram"
    r = requests.get(TG_SET_WEBHOOK_URL, params={"url": url}, timeout=15)
    try:
        return jsonify(r.json()), r.status_code
    except Exception:
        return jsonify({"ok": False, "raw": r.text}), r.status_code

@app.post("/telegram")
def telegram_webhook():
    data = request.get_json(silent=True) or {}

    # a) Callback_query (si más adelante usas botones inline)
    if data.get("callback_query"):
        return jsonify({"ok": True})

    # b) Mensajes / Comandos
    message = data.get("message") or data.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat_id = (message.get("chat") or {}).get("id")
    text = (message.get("text") or "").strip()
    username = (message.get("from") or {}).get("username")

    if not text:
        return jsonify({"ok": True})

    # Normalizamos comandos (acepta con o sin slash)
    t = text.upper().strip()
    if t.startswith("/"):
        t = t[1:]

    # ====== COMANDO: START (bienvenida + menú) ======
    if t in ("START",):
        welcome = (
            "✨ *Bienvenido a PureMuse Bot*\n\n"
            "Explora nuestras galerías y suscríbete al plan VIP para recibir *1 enlace diario* durante *30 días*."
        )
        tg_send_text(chat_id, welcome, parse_mode="Markdown", reply_markup=build_main_menu())
        return jsonify({"ok": True})

    # ====== COMANDO: ABOUT ======
    if t in ("ABOUT",):
        tg_send_text(chat_id,
            "👋 *ABOUT*\n\nPureMuse ofrece galerías artísticas exclusivas.\n"
            "Con VIP recibes un enlace diario por 30 días.\n\nUsa *BUY VIP* para suscribirte.",
            parse_mode="Markdown",
            reply_markup=build_main_menu())
        return jsonify({"ok": True})

    # ====== COMANDO: GALLERIES ======
    if t in ("GALLERIES",):
        tg_send_text(chat_id,
            "🖼️ *GALLERIES*\n\nLas galerías VIP se envían *1 por día* durante *30 días*.\n"
            "Los enlaces se hospedan en Google Drive.\n\nCompra con *BUY VIP*.",
            parse_mode="Markdown",
            reply_markup=build_main_menu())
        return jsonify({"ok": True})

    # ====== COMANDO: BUY VIP (link MP $50 MXN) ======
    if t in ("BUY VIP","BUY_VIP","BUYVIP"):
        try:
            link = mp_create_link(chat_id)
            msg = (
                "💳 *BUY VIP*\n\nSuscripción de *30 días* por *$50 MXN*.\n\n"
                f"Completa tu pago aquí:\n{link}\n\n"
                "Al aprobarse, activamos tu VIP y enviamos la *Galería Día 1* automáticamente."
            )
            tg_send_text(chat_id, msg, parse_mode="Markdown", reply_markup=build_main_menu())
        except Exception as e:
            tg_send_text(chat_id, "⚠️ No pude generar el link de pago. Intenta de nuevo en unos minutos.")
        return jsonify({"ok": True})

    # ====== COMANDO: VIP STATUS ======
    if t in ("VIP STATUS","VIP_STATUS","VIPSTATUS"):
        ensure_tables()
        with SessionLocal() as db:
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if not u:
                tg_send_text(chat_id,
                    "❌ No tienes VIP activo. Usa *BUY VIP* para suscribirte.",
                    parse_mode="Markdown",
                    reply_markup=build_main_menu())
            else:
                active = is_active_vip(u.start_date)
                dias_rest = max(0, 30 - (date.today() - u.start_date).days)
                tg_send_text(chat_id,
                    f"👤 *VIP STATUS*\n\nEstado: {'ACTIVO ✅' if active else 'VENCIDO ❌'}\n"
                    f"Inicio: {u.start_date}\n"
                    f"Día actual: {u.progress_day+1}/30\n"
                    f"Días restantes: {dias_rest}\n\n"
                    f"{'¡Sigue atento a tu galería diaria!' if active else 'Renueva con BUY VIP.'}",
                    parse_mode="Markdown",
                    reply_markup=build_main_menu())
        return jsonify({"ok": True})

    # Fallback: re-muestra menú
    tg_send_text(chat_id, "Usa el menú para navegar.", reply_markup=build_main_menu())
    return jsonify({"ok": True})

@app.route("/mp/webhook", methods=["POST", "GET"])
def mp_webhook():
    """
    Mercado Pago envía notificaciones con ?type=payment & data.id=PAYMENT_ID.
    Activamos VIP y enviamos Día 1 al aprobarse.
    """
    secret = request.args.get("secret")
    if secret != MP_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 403

    payload = request.get_json(silent=True) or {}
    type_ = request.args.get("type") or payload.get("type")
    data_id = request.args.get("data.id") or (payload.get("data", {}) or {}).get("id")

    if type_ == "payment" and data_id:
        info = mp_fetch_payment(str(data_id))
        if info and info.get("status") == "approved":
            ext = info.get("external_reference")  # guardamos chat_id aquí
            try:
                chat_id = int(ext)
            except Exception:
                chat_id = None

            if chat_id:
                ensure_tables()
                with SessionLocal() as db:
                    u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
                    if u:
                        # renovar ciclo: reinicia día 0 desde hoy
                        u.start_date = date.today()
                        u.progress_day = 0
                        db.commit()
                    else:
                        u = VIPUser(chat_id=chat_id, username=None, start_date=date.today(), progress_day=0)
                        db.add(u); db.commit(); db.refresh(u)

                # Enviar día 1 y avanzar a 1
                send_gallery_today(chat_id, 0)
                with SessionLocal() as db:
                    u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one()
                    u.progress_day = 1
                    db.commit()

    return jsonify({"ok": True})

@app.get("/cron/daily")
def cron_daily():
    """
    Llamar 1 vez al día (Render → Jobs).
    Envía 1 galería y avanza progress_day a todos los VIP activos.
    Tras 30 días (progress_day llega a 30), ya no avanza más y el usuario debe comprar de nuevo.
    """
    ensure_tables()
    sent = 0
    today = date.today()
    with SessionLocal() as db:
        users = db.execute(select(VIPUser)).scalars().all()
        for u in users:
            if is_active_vip(u.start_date, today):
                # Enviar galería del día actual
                send_gallery_today(u.chat_id, u.progress_day)
                # Avanzar (máx 29 → día 30)
                u.progress_day = min(29, (u.progress_day or 0) + 1)
                db.commit()
                sent += 1
    return jsonify({"ok": True, "sent": sent, "date": str(today)}), 200

@app.get("/paid")
def paid_landing():
    status = request.args.get("status", "unknown")
    return f"Pago: {status}. Puedes cerrar esta pestaña y volver a Telegram.", 200

# Crear tablas al arrancar
with app.app_context():
    ensure_tables()
