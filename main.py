
# Fixed main.py for X_Reward_Bot (patched)
# Changes:
# - Validate uploaded images (max 5 MB) using Pillow
# - Store only filename in DB (not absolute paths)
# - Return public /uploads/{fname} URLs in my_submissions
# - Add simple ad_offer / verify_ad stub endpoints to integrate ad providers
# - Minor hardening: require BOT_TOKEN in production (ENV=production)
import os
import sqlite3
import hmac
import hashlib
import json
import datetime
import pathlib
from urllib.parse import parse_qsl
import multiprocessing

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging
import io

# Image validation
from PIL import Image, UnidentifiedImageError

# Try to import telegram, but continue if not available
try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler
except Exception:
    InlineKeyboardButton = InlineKeyboardMarkup = WebAppInfo = Bot = None
    Application = CommandHandler = CallbackQueryHandler = None

import asyncio
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "X_Reward_Bot").strip().lstrip("@")
ADMINS_ENV = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in ADMINS_ENV.split(",") if x.strip().isdigit()] or []
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/X_Reward_botChannel")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/yoursupportgroup")
PORT = int(os.getenv("PORT", "8080"))
ENV = os.getenv("ENV", "development").lower()

# DATA_DIR with fallback
_data_dir = os.getenv("DATA_DIR", "/data")
try:
    if not os.path.exists(_data_dir):
        os.makedirs(_data_dir, exist_ok=True)
    DATA_DIR = _data_dir
except Exception as e:
    logger.warning("Cannot create DATA_DIR at %s: %s — falling back to /tmp/data", _data_dir, e)
    DATA_DIR = "/tmp/data"
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, os.getenv("DB_PATH", "bot.db"))
UPLOAD_DIR = os.path.join(DATA_DIR, os.getenv("UPLOAD_DIR", "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

if ENV == "production" and not BOT_TOKEN:
    logger.error("Running in production but BOT_TOKEN is not set. Exiting for safety.")
    raise SystemExit("BOT_TOKEN required in production")

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN not set. init_data verification will be skipped (dev/test).")

# Telegram bot client (optional)
try:
    tg_bot = Bot(token=BOT_TOKEN) if BOT_TOKEN and Bot else None
except Exception as e:
    logger.warning("Failed to init telegram Bot client: %s", e)
    tg_bot = None

# DB helpers
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def migrate():
    con = get_db(); cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id INTEGER PRIMARY KEY,
      username TEXT,
      coins INTEGER DEFAULT 0,
      referrer_id INTEGER,
      joined_at TEXT,
      ads_watched INTEGER DEFAULT 0,
      ad_counter INTEGER DEFAULT 0,
      boost_until TEXT,
      last_daily TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      task_id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT,
      description TEXT,
      link TEXT,
      reward INTEGER
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_submissions (
      submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER,
      task_id INTEGER,
      file_path TEXT,
      status TEXT DEFAULT 'pending',
      submitted_at TEXT,
      reviewed_by INTEGER,
      review_reason TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS verifiers (
      verifier_id INTEGER PRIMARY KEY
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
      referrer_id INTEGER,
      referred_id INTEGER,
      PRIMARY KEY (referrer_id, referred_id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT
    )""")
    # insert default settings if not present
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("channel_link", CHANNEL_LINK))
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("support_link", SUPPORT_LINK))
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", ("bot_username", BOT_USERNAME))
    con.commit(); con.close()
    logger.info("DB migrated")

def get_setting(key, default=None):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    r = cur.fetchone(); con.close()
    return r["value"] if r else default

def set_setting(key, value):
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    con.commit(); con.close()

# init_data verification
def verify_init_data(init_data: str, bot_token: str) -> dict:
    if not init_data:
        raise ValueError("Missing init_data")
    parsed = dict(parse_qsl(init_data, strict_parsing=False))
    check_hash = parsed.pop('hash', None)
    if check_hash:
        if bot_token:
            data_check_list = [f"{k}={parsed[k]}" for k in sorted(parsed.keys())]
            data_check_string = "\n".join(data_check_list)
            secret_key = hashlib.sha256(bot_token.encode()).digest()
            calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
            if calculated_hash != check_hash:
                raise ValueError("Invalid init_data hash")
        else:
            logger.warning("BOT_TOKEN unset: skipping init_data hash verification (dev mode).")
    if 'user' in parsed:
        try:
            parsed['user'] = json.loads(parsed['user'])
        except Exception:
            pass
    return parsed

DEV_SKIP_MEMBERSHIP = os.getenv("DEV_SKIP_MEMBERSHIP", "").lower() in ("1", "true", "yes")

def extract_channel_username(link: str) -> str:
    return link.rstrip('/').split('/')[-1]

def is_member_of_channel(user_id: int) -> bool:
    if DEV_SKIP_MEMBERSHIP:
        return True
    if not tg_bot:
        return False
    channel = extract_channel_username(get_setting("channel_link", CHANNEL_LINK))
    if not channel:
        return False
    chat_id = channel if channel.startswith('@') else f"@{channel}"
    try:
        member = tg_bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.debug("Channel membership check error: %s", e)
        return False

# FastAPI app
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/", response_class=HTMLResponse)
async def index():
    if os.path.exists("index.html"):
        return HTMLResponse(open("index.html", "r", encoding="utf-8").read())
    return HTMLResponse("<h3>Upload index.html to project root.</h3>")

@app.get("/uploads/{fname}")
async def serve_upload(fname: str):
    safe = os.path.join(UPLOAD_DIR, os.path.basename(fname))
    if not os.path.exists(safe):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(safe)

# get user & basic info
@app.post("/webapp/me")
async def webapp_me(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, joined_at) VALUES (?, ?, ?)", (uid, user.get("username") or user.get("first_name") or f"user{uid}", datetime.datetime.utcnow().isoformat()))
    cur.execute("SELECT coins, ads_watched, boost_until FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone(); con.close()
    if not row:
        coins = 0; ads_watched = 0; boost_until = None
    else:
        coins, ads_watched, boost_until = row
    channel = get_setting("channel_link", CHANNEL_LINK)
    support = get_setting("support_link", SUPPORT_LINK)
    bot_username = get_setting("bot_username", BOT_USERNAME)
    referral_link = f"https://t.me/{bot_username}?start={uid}"
    is_admin = uid in ADMIN_IDS
    return {"ok": True, "coins": coins, "ads_watched": ads_watched, "boost_until": boost_until, "channel": channel, "support": support, "referral_link": referral_link, "is_admin": is_admin}

# Ad endpoints
@app.post("/webapp/ad_watched")
async def ad_watched(req: Request):
    payload = await req.json()
    init_data = payload.get("init_data", "")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if not is_member_of_channel(uid):
        return JSONResponse({"ok": False, "error": "join_channel", "channel": get_setting("channel_link", CHANNEL_LINK)})
    username = user.get("username") or user.get("first_name") or f"user{uid}"
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, ads_watched, ad_counter) VALUES (?, ?, 0, 0, 0)", (uid, username))
    coins_awarded = 100
    cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ?, ads_watched = COALESCE(ads_watched,0) + 1 WHERE user_id = ?", (coins_awarded, uid))
    cur.execute("SELECT COALESCE(ad_counter,0) FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    ad_counter = (row[0] or 0) + 1
    boost_activated = None
    if ad_counter >= 3:
        until = (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).isoformat() + "Z"
        cur.execute("UPDATE users SET boost_until = ?, ad_counter = 0 WHERE user_id = ?", (until, uid))
        boost_activated = until
        ads_to_next = 3
    else:
        cur.execute("UPDATE users SET ad_counter = ? WHERE user_id = ?", (ad_counter, uid))
        ads_to_next = 3 - ad_counter
    con.commit()
    cur.execute("SELECT coins, ads_watched FROM users WHERE user_id = ?", (uid,))
    coins, ads_watched = cur.fetchone()
    con.close()
    return {"ok": True, "coins_awarded": coins_awarded, "coins_total": coins, "ads_watched": ads_watched, "ads_to_next_boost": ads_to_next, "boost_until": boost_activated}

# Simple ad offer endpoint (client can call to get an ad id / provider info)
@app.post("/webapp/ad_offer")
async def ad_offer(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    # This is a lightweight stub. Replace with real ad-network integration on client side.
    ad_id = f"mock_ad_{int(datetime.datetime.utcnow().timestamp())}"
    return {"ok": True, "ad_id": ad_id, "provider": "mock", "reward": 100, "instruction": "Open a rewarded ad using your ad SDK, then call /webapp/verify_ad with the ad_receipt."}

@app.post("/webapp/verify_ad")
async def verify_ad(payload: dict):
    init_data = payload.get("init_data")
    ad_receipt = payload.get("ad_receipt")
    if not init_data or not ad_receipt:
        raise HTTPException(status_code=400, detail="init_data and ad_receipt required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    # In real integration, verify ad_receipt with ad provider. Here we accept any non-empty receipt.
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, ads_watched, ad_counter) VALUES (?, ?, 0, 0, 0)", (uid, user.get("username") or user.get("first_name") or f"user{uid}"))
    coins_awarded = 100
    cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ?, ads_watched = COALESCE(ads_watched,0) + 1 WHERE user_id = ?", (coins_awarded, uid))
    con.commit()
    cur.execute("SELECT coins, ads_watched FROM users WHERE user_id = ?", (uid,))
    coins, ads_watched = cur.fetchone()
    con.close()
    return {"ok": True, "coins_awarded": coins_awarded, "coins_total": coins, "ads_watched": ads_watched}

# File upload constraints
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

@app.post("/webapp/submit_proof")
async def submit_proof(init_data: str = Form(...), task_id: int = Form(...), file: UploadFile = File(...)):
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if not is_member_of_channel(uid):
        raise HTTPException(status_code=403, detail="join_channel")
    # read bytes and validate size
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty_file")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="file_too_big")
    ext = pathlib.Path(file.filename or "").suffix.lower() or ".jpg"
    if ext not in ALLOWED_EXT:
        # still allow but try to validate as image; reject otherwise
        raise HTTPException(status_code=400, detail="invalid_extension")
    # validate image with Pillow
    try:
        img = Image.open(io.BytesIO(content))
        img.verify()  # will raise if not an image
    except UnidentifiedImageError:
        raise HTTPException(status_code=400, detail="invalid_image")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_image")
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    fname = f"{uid}_{ts}{ext}"
    fpath = os.path.join(UPLOAD_DIR, fname)
    # write bytes to disk
    with open(fpath, "wb") as f:
        f.write(content)
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (uid, user.get("username") or user.get("first_name") or f"user{uid}"))
    # store only filename (not full path) for privacy
    cur.execute("INSERT INTO task_submissions (user_id, task_id, file_path, submitted_at) VALUES (?, ?, ?, ?)", (uid, task_id, fname, datetime.datetime.utcnow().isoformat()))
    con.commit(); con.close()
    return {"ok": True, "msg": "Proof submitted (image). Waiting for review.", "file_url": f"/uploads/{fname}"}

@app.post("/webapp/my_submissions")
async def my_submissions(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT submission_id, task_id, file_path, status, submitted_at, reviewed_by, review_reason FROM task_submissions WHERE user_id = ? ORDER BY submission_id DESC", (uid,))
    rows = cur.fetchall(); con.close()
    subs = []
    for r in rows:
        fname = r["file_path"]
        file_url = f"/uploads/{fname}" if fname else None
        subs.append({
            "submission_id": r["submission_id"],
            "task_id": r["task_id"],
            "file_url": file_url,
            "status": r["status"],
            "submitted_at": r["submitted_at"],
            "reviewed_by": r["reviewed_by"],
            "review_reason": r["review_reason"]
        })
    return {"ok": True, "submissions": subs}

# Minimal leaderboard endpoint
@app.get("/webapp/leaderboard")
async def leaderboard(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100)):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT user_id, username, coins FROM users ORDER BY coins DESC LIMIT ? OFFSET ?", (per_page, (page-1)*per_page))
    rows = cur.fetchall(); con.close()
    lb = [{"user_id": r["user_id"], "username": r["username"], "coins": r["coins"]} for r in rows]
    return {"ok": True, "leaderboard": lb, "page": page, "per_page": per_page}

# tasks add/edit/delete (admin only)
@app.post("/webapp/add_task")
async def add_task(payload: dict):
    init_data = payload.get("init_data"); title = payload.get("title")
    description = payload.get("description", ""); link = payload.get("link", ""); reward = int(payload.get("reward") or 0)
    if not init_data or not title:
        raise HTTPException(status_code=400, detail="init_data and title required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT INTO tasks (title, description, link, reward) VALUES (?, ?, ?, ?)", (title, description, link, reward))
    con.commit(); con.close()
    return {"ok": True, "msg": "task_added"}

@app.post("/webapp/edit_task")
async def edit_task(payload: dict):
    init_data = payload.get("init_data"); task_id = payload.get("task_id")
    title = payload.get("title"); description = payload.get("description", ""); link = payload.get("link", ""); reward = payload.get("reward")
    if not init_data or not task_id:
        raise HTTPException(status_code=400, detail="init_data and task_id required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("UPDATE tasks SET title = COALESCE(?,title), description = COALESCE(?,description), link = COALESCE(?,link), reward = COALESCE(?,reward) WHERE task_id = ?", (title, description, link, reward, task_id))
    con.commit(); con.close()
    return {"ok": True, "msg": "task_updated"}

@app.post("/webapp/delete_task")
async def delete_task(payload: dict):
    init_data = payload.get("init_data"); task_id = payload.get("task_id")
    if not init_data or not task_id:
        raise HTTPException(status_code=400, detail="init_data and task_id required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    cur.execute("DELETE FROM task_submissions WHERE task_id = ?", (task_id,))
    con.commit(); con.close()
    return {"ok": True, "msg": "task_deleted"}

# other settings endpoints kept as before
@app.get("/webapp/settings")
async def webapp_get_settings():
    return {"ok": True, "channel_link": get_setting("channel_link", CHANNEL_LINK), "support_link": get_setting("support_link", SUPPORT_LINK), "bot_username": get_setting("bot_username", BOT_USERNAME)}

@app.post("/webapp/update_channel")
async def webapp_update_channel(payload: dict):
    init_data = payload.get("init_data"); new = payload.get("channel")
    if not init_data or not new:
        raise HTTPException(status_code=400, detail="init_data and channel required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    set_setting("channel_link", new)
    return {"ok": True, "msg": "channel_updated", "channel": new}

@app.post("/webapp/update_support")
async def webapp_update_support(payload: dict):
    init_data = payload.get("init_data"); new = payload.get("support")
    if not init_data or not new:
        raise HTTPException(status_code=400, detail="init_data and support required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    set_setting("support_link", new)
    return {"ok": True, "msg": "support_updated", "support": new}

# --- Tasks listing endpoint (used by the front-end) ---
from fastapi import Query
@app.get("/webapp/get_tasks")
async def webapp_get_tasks(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100)):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT task_id, title, description, link, reward FROM tasks ORDER BY task_id DESC LIMIT ? OFFSET ?", (per_page, (page-1)*per_page))
    rows = cur.fetchall(); con.close()
    tasks = [{"task_id": r["task_id"], "title": r["title"], "description": r["description"], "link": r["link"], "reward": r["reward"]} for r in rows]
    return {"ok": True, "tasks": tasks, "page": page, "per_page": per_page}

# --- Daily claim endpoint ---
@app.post("/webapp/daily_claim")
async def webapp_daily_claim(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, last_daily) VALUES (?, ?, 0, NULL)", (uid, user.get("username") or user.get("first_name") or f"user{uid}"))
    cur.execute("SELECT last_daily, coins FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone()
    last_daily = row["last_daily"] if row else None
    coins = row["coins"] if row else 0
    # check UTC date
    today = datetime.datetime.utcnow().date()
    claimed = False
    if last_daily:
        try:
            last_date = datetime.datetime.fromisoformat(last_daily).date()
        except Exception:
            last_date = None
        if last_date == today:
            con.close()
            return {"ok": False, "error": "already_claimed", "message": "Daily reward already claimed today."}
    reward = 50
    coins += reward
    cur.execute("UPDATE users SET coins = ?, last_daily = ? WHERE user_id = ?", (coins, datetime.datetime.utcnow().isoformat(), uid))
    con.commit(); con.close()
    return {"ok": True, "reward": reward, "coins_total": coins}

# --- Admin: list pending submissions and review (approve/reject) ---
@app.get("/webapp/pending_submissions")
async def pending_submissions(payload: dict = None):
    # payload optional; admin identity verified via init_data in query param or JSON body
    data = {}
    if payload and isinstance(payload, dict):
        data = payload
    # allow admin to pass init_data via query params too
    init_data = data.get("init_data") or ""
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("""SELECT ts.submission_id, ts.user_id, u.username, ts.task_id, ts.file_path, ts.status, ts.submitted_at
                   FROM task_submissions ts
                   LEFT JOIN users u ON u.user_id = ts.user_id
                   WHERE ts.status = 'pending' ORDER BY ts.submission_id ASC""")
    rows = cur.fetchall(); con.close()
    items = []
    for r in rows:
        fname = r["file_path"]
        file_url = f"/uploads/{fname}" if fname else None
        items.append({"submission_id": r["submission_id"], "user_id": r["user_id"], "username": r["username"], "task_id": r["task_id"], "file_url": file_url, "status": r["status"], "submitted_at": r["submitted_at"]})
    return {"ok": True, "pending": items}

@app.post("/webapp/review_submission")
async def review_submission(payload: dict):
    init_data = payload.get("init_data"); submission_id = payload.get("submission_id"); action = payload.get("action"); reason = payload.get("reason", "")
    if not init_data or not submission_id or not action:
        raise HTTPException(status_code=400, detail="init_data, submission_id and action required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {}); uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    action = action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="invalid_action")
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT submission_id, user_id, task_id, file_path, status FROM task_submissions WHERE submission_id = ?", (submission_id,))
    row = cur.fetchone()
    if not row:
        con.close()
        raise HTTPException(status_code=404, detail="submission_not_found")
    if row["status"] != "pending":
        con.close()
        return {"ok": False, "error": "already_reviewed", "status": row["status"]}
    reviewed_at = datetime.datetime.utcnow().isoformat()
    if action == "approve":
        # credit user with task reward (if task exists)
        task_id = row["task_id"]
        cur.execute("SELECT reward FROM tasks WHERE task_id = ?", (task_id,))
        t = cur.fetchone()
        reward = t["reward"] if t else 0
        cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ? WHERE user_id = ?", (reward, row["user_id"]))
        cur.execute("UPDATE task_submissions SET status = 'approved', reviewed_by = ?, review_reason = ?, submitted_at = submitted_at WHERE submission_id = ?", (uid, reason, submission_id))
        con.commit()
        cur.execute("SELECT coins FROM users WHERE user_id = ?", (row["user_id"],))
        coins_total = cur.fetchone()["coins"]
        con.close()
        return {"ok": True, "msg": "approved", "reward": reward, "user_coins": coins_total}
    else:
        cur.execute("UPDATE task_submissions SET status = 'rejected', reviewed_by = ?, review_reason = ? WHERE submission_id = ?", (uid, reason, submission_id))
        con.commit(); con.close()
        return {"ok": True, "msg": "rejected"}

# Bot functions (start button sends webapp button) — unchanged, minimal
async def bot_start(update, context):
    try:
        u = update.effective_user
    except Exception:
        u = getattr(update, 'user', None) or {'id': None, 'first_name': 'User'}
    args = getattr(context, "args", []) or []
    try:
        uid = u.id if hasattr(u, 'id') else int(u.get('id') if isinstance(u, dict) else 0)
    except Exception:
        uid = 0
    # ensure user in DB
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, joined_at) VALUES (?, ?, 100, ?)", (uid, getattr(u, 'username', None) or (u.get('username') if isinstance(u, dict) else None) or getattr(u, 'first_name', None) or f"user{uid}", datetime.datetime.utcnow().isoformat()))
    con.commit(); con.close()
    kb = []
    if WEBAPP_URL and InlineKeyboardButton and WebAppInfo:
        try:
            kb.append([InlineKeyboardButton("🌐 Open Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])
        except Exception:
            logger.debug("Could not create WebAppInfo button.")
    if InlineKeyboardButton:
        kb += [
            [InlineKeyboardButton("📋 Tasks", callback_data="tasks"), InlineKeyboardButton("💰 Coins", callback_data="coins")],
            [InlineKeyboardButton("🎁 Daily Reward", callback_data="daily"), InlineKeyboardButton("🤝 Refer", callback_data="refer")],
            [InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"), InlineKeyboardButton("👥 Top Inviters", callback_data="top_inviters")],
            [InlineKeyboardButton("🛠️ Admin", callback_data="admin")]
        ]
    reply_markup = InlineKeyboardMarkup(kb) if InlineKeyboardMarkup and kb else None
    try:
        if hasattr(context, 'bot') and context.bot and reply_markup:
            await context.bot.send_message(chat_id=uid, text=f"Welcome! Use the buttons below.", reply_markup=reply_markup)
    except Exception as e:
        logger.warning("Could not send start message with webapp button: %s", e)

async def bot_callback(update, context):
    try:
        q = update.callback_query; await q.answer()
        await q.edit_message_text("Callback received.")
    except Exception:
        pass

def run_bot():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN missing — bot will not start.")
        return
    async def _main():
        application = Application.builder().token(BOT_TOKEN).build()
        application.add_handler(CommandHandler("start", bot_start))
        application.add_handler(CallbackQueryHandler(bot_callback))
        logger.info("Bot polling starting...")
        await application.run_polling()
    try:
        asyncio.run(_main())
    except Exception as e:
        logger.exception("Bot process crashed: %s", e)

if __name__ == "__main__":
    migrate()
    # start bot in separate process
    if BOT_TOKEN:
        try:
            p = multiprocessing.Process(target=run_bot)
            p.start()
            logger.info("Bot process started (PID %s)", p.pid)
        except Exception as e:
            logger.exception("Failed to start bot process: %s", e)
    else:
        logger.info("BOT_TOKEN not set — bot will not start (dev mode).")
    logger.info(f"Starting HTTP server on port {PORT} ...")
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv('PORT', PORT)), log_level="info")
