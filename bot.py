from flask import Flask, request, jsonify
import os, sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict
import requests

# ============== EDITA SOLO ESTAS 3 VARIABLES ==============
TOKEN = "8280812701:AAGH4X-HoahE_jA6foiV0oo61CQrMuLd9hM"                           # p.ej. 12345:ABCDEF...
BASE_URL = "https://puremusebot.onrender.com"        # URL pública de Render (https)
MP_ACCESS_TOKEN = "APP_USR-3510033415376991-101723-4123f543520272287c00983a3ca15c83-95374565"                # Access Token de Mercado Pago (test/prod)
# =========================================================

VIP_DURATION_SECONDS = 30 * 24 * 3600              # 30 días
DB_PATH = "/mnt/data/puremuse.sqlite3"             # Persistencia Render

SEND_URL     = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
MP_PREFS_URL = "https://api.mercadopago.com/checkout/preferences"
MP_PAY_URL   = "https://api.mercadopago.com/v1/payments/"

app = Flask(__name__)

# -------------------- Utils --------------------
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

def seconds_to_dhm(secs: int):
    d = secs // 86400; h = (secs % 86400) // 3600; m = (secs % 3600) // 60
    return d, h, m

# -------------------- Estado de BD (con fallback) --------------------
USE_DB = False  # se activa si DB inicia bien

# Memoria (fallback cuando no hay DB)
_mem_vip_access: Dict[int, Dict[str, int]] = {}     # chat_id -> {"until": epoch, "status": 1}
_mem_progress: Dict[int, int] = {}                  # chat_id -> last_gallery_id
_mem_galleries: List[Tuple[int, str, str, int]] = []# (id, url, title, active)
_mem_auto_id = 0

def _mem_add_gallery(url: str, title: Optional[str]):
    global _mem_auto_id
    # evitar duplicados por url
    for (gid, u, t, a) in _mem_galleries:
        if u == url:
            return
    _mem_auto_id += 1
    _mem_galleries.append((_mem_auto_id, url, title or "", 1))

# -------------------- DB helpers --------------------
def db_init():
    """Intenta inicializar SQLite. Si falla, deja USE_DB = False (se usa memoria)."""
    global USE_DB
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_access (
            chat_id INTEGER PRIMARY KEY,
            access_until INTEGER NOT NULL,
            last_payment_id TEXT,
            status TEXT NOT NULL
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS galleries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_progress (
            chat_id INTEGER PRIMARY KEY,
            last_gallery_id INTEGER
        );
        """)
        con.commit(); con.close()
        USE_DB = True
        app.logger.info("✅ DB initialized (SQLite) — USE_DB=True")
    except Exception as e:
        USE_DB = False
        app.logger.error(f"⚠️ DB init failed, using in-memory fallback. Reason: {e}")

def db_upsert_vip(chat_id: int, access_until_epoch: int, payment_id: Optional[str]):
    if USE_DB:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("""
          INSERT INTO vip_access (chat_id, access_until, last_payment_id, status)
          VALUES (?, ?, ?, 'active')
          ON CONFLICT(chat_id) DO UPDATE SET
            access_until=excluded.access_until,
            last_payment_id=excluded.last_payment_id,
            status='active';
        """, (chat_id, access_until_epoch, payment_id or ""))
        con.commit(); con.close()
    else:
        _mem_vip_access[chat_id] = {"until": access_until_epoch, "status": 1}

def db_get_vip(chat_id: int):
    if USE_DB:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("SELECT access_until, status FROM vip_access WHERE chat_id=?", (chat_id,))
        row = cur.fetchone(); con.close()
        return row  # (access_until, status_str)
    else:
        rec = _mem_vip_access.get(chat_id)
        if not rec: return None
        return (rec["until"], "active" if rec["status"] == 1 else "inactive")

def db_set_progress(chat_id: int, last_gallery_id: int):
    if USE_DB:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("""
          INSERT INTO vip_progress (chat_id, last_gallery_id) VALUES (?, ?)
          ON CONFLICT(chat_id) DO UPDATE SET last_gallery_id=excluded.last_gallery_id;
        """, (chat_id, last_gallery_id))
        con.commit(); con.close()
    else:
        _mem_progress[chat_id] = last_gallery_id

def db_get_progress(chat_id: int):
    if USE_DB:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("SELECT last_gallery_id FROM vip_progress WHERE chat_id=?", (chat_id,))
        row = cur.fetchone(); con.close()
        return row[0] if row else None
    else:
        return _mem_progress.get(chat_id)

def db_add_gallery(url: str, title: Optional[str]):
    if USE_DB:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO galleries (url, title, active, created_at) VALUES (?, ?, 1, ?)",
                    (url, title or "", now_epoch()))
        con.commit(); con.close()
    else:
        _mem_add_gallery(url, title)

def db_list_galleries():
    if USE_DB:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("SELECT id, url, title, active FROM galleries ORDER BY id ASC")
        rows = cur.fetchall(); con.close()
        return rows
    else:
        return _mem_galleries[:]  # copia

def db_next_gallery_for(chat_id: int):
    """Siguiente galería ACTIVA (id asc) que el usuario no recibió aún."""
    last_id = db_get_progress(chat_id)
    rows = db_list_galleries()
    rows = [r for r in rows if r[3] == 1]  # active == 1
    if not rows:
        return None
    if last_id is None:
        return rows[0][:3]  # (id, url, title)
    for (gid, url, title, active) in rows:
        if gid > last_id:
            return (gid, url, title)
    return None

# -------------------- Sincronizar galleries.txt --------------------
def sync_galleries_from_file(file_path="galleries.txt"):
    try:
        if not os.path.exists(file_path):
            app.logger.warning("No se encontró galleries.txt")
            return
        added = 0
        with open(file_path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"): continue
                parts = line.split("|")
                if len(parts) == 2:
                    title, url = parts[0].strip(), parts[1].strip()
                else:
                    title, url = "", parts[0].strip()
                if url.startswith("http"):
                    db_add_gallery(url, title); added += 1
        app.logger.info(f"✅ Sincronizadas {added} entradas desde galleries.txt (USE_DB={USE_DB})")
    except Exception as e:
        app.logger.error(f"sync_galleries_from_file error: {e}")

# -------------------- Mercado Pago --------------------
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

# -------------------- Textos --------------------
WELCOME = "🌹 *Welcome to PureMuse.*\nWhere art meets sensuality.\n\nChoose one of the options below 👇"
ABOUT   = "*PureMuse* is a digital gallery where art and sensuality merge.\nExclusive photographic collections, elegant aesthetics, and the beauty of desire."
COLLECT = "🖼️ *PureMuse Collections*\n• Noir & Gold Edition\n• Veils & Silhouettes\n• Amber Light\n_(Demo)_"
HELP    = "Available commands:\n/start, /about, /collections, /pay, /content, /vip, /renew, /support, /help"
PAID_OK = "✅ Payment received. VIP access is *active for 30 days*.\nUse `/content` to get today’s gallery, or `/vip` to check your status."

# -------------------- Rutas --------------------
@app.route("/", methods=["GET"])
def home():
    return f"PureMuse Bot is up ✨ Stage 3 SAFE (USE_DB={USE_DB})", 200

@app.route("/healthz", methods=["GET"])
def health():
    return jsonify({"ok": True, "use_db": USE_DB}), 200

@app.route("/webhook", methods=["POST","GET"])
def webhook():
    if request.method == "GET": return "Webhook OK", 200
    data = request.get_json(silent=True) or {}
    msg  = data.get("message") or data.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id"); text = (msg.get("text") or "").strip().lower()
    if not chat_id or not text: return "OK", 200

    if text.startswith("/start"):
        keyboard = {
            "keyboard": [
                [{"text": "/about"}, {"text": "/collections"}],
                [{"text": "/pay"}, {"text": "/content"}],
                [{"text": "/vip"}, {"text": "/support"}]
            ],
            "resize_keyboard": True, "one_time_keyboard": False
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
            if remaining > 0 and (status == "active" or status == 1):
                d,h,m = seconds_to_dhm(remaining)
                tg_send(chat_id, f"✅ VIP active. Remaining: *{d}d {h}h {m}m*.")
            else:
                tg_send(chat_id, "⛔ VIP expired. Use /renew to reactivate.")

    elif text.startswith("/content"):
        rec = db_get_vip(chat_id)
        if not rec:
            tg_send(chat_id, "❌ No VIP access. Use /pay to subscribe.")
        else:
            access_until, status = rec
            if (access_until - now_epoch()) <= 0 or (status not in ("active", 1)):
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

@app.route("/mp/webhook", methods=["POST","GET"])
def mp_webhook():
    if request.method == "GET": return "MP Webhook OK", 200
    body = request.get_json(silent=True) or {}
    topic = body.get("type") or body.get("topic")
    payment_id = (body.get("data") or {}).get("id")
    if topic == "payment" and payment_id:
        pay = mp_get_payment(payment_id)
        if pay:
            status  = pay.get("status")
            ext_ref = pay.get("external_reference")
            try: chat_id = int(ext_ref) if ext_ref else None
            except: chat_id = None
            app.logger.info(f"[MP] Payment {payment_id} -> {status} (chat_id={chat_id})")
            if status == "approved" and chat_id:
                access_until = now_epoch() + VIP_DURATION_SECONDS
                db_upsert_vip(chat_id, access_until, str(payment_id))
                tg_send(chat_id, PAID_OK)
    return jsonify({"status":"received"}), 200

@app.route("/cron/daily", methods=["GET","POST"])
def cron_daily():
    # En SAFE, si no hay DB: usa memoria y envía a los que estén marcados
    sent = 0
    now_e = now_epoch()
    users: List[int] = []

    if USE_DB:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("SELECT chat_id, access_until, status FROM vip_access WHERE access_until > ? AND status='active'", (now_e,))
        rows = cur.fetchall(); con.close()
        for chat_id, access_until, status in rows:
            users.append(chat_id)
    else:
        for chat_id, rec in _mem_vip_access.items():
            if rec["status"] == 1 and rec["until"] > now_e:
                users.append(chat_id)

    for chat_id in users:
        nxt = db_next_gallery_for(chat_id)
        if not nxt: continue
        gid, url, title = nxt
        title_txt = f"*{title}*\n" if title else ""
        tg_send(chat_id, f"🌙 *Daily VIP drop*\n{title_txt}{url}")
        db_set_progress(chat_id, gid)
        sent += 1

    return jsonify({"active_users": len(users), "sent": sent, "use_db": USE_DB}), 200

# -------------------- Bootstrap --------------------
db_init()                 # NO revienta si falla; cae a memoria
sync_galleries_from_file()# NO revienta si falta/está vacío (loggea)
