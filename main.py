# main.py
import os
import sqlite3
import threading
import hmac
import hashlib
import json
import datetime
import pathlib
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import logging

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
except Exception:
    Update = InlineKeyboardButton = InlineKeyboardMarkup = WebAppInfo = Bot = None
    Application = CommandHandler = CallbackQueryHandler = ContextTypes = None

import asyncio
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "X_Reward_Bot").strip()
ADMINS_ENV = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x) for x in ADMINS_ENV.split(",") if x.strip().isdigit()] or []
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/X_Reward_botChannel")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/xrewardchannel")
PORT = int(os.getenv("PORT", "8080"))

DATA_DIR = os.getenv("DATA_DIR", "/data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, os.getenv("DB_PATH", "bot.db"))
UPLOAD_DIR = os.path.join(DATA_DIR, os.getenv("UPLOAD_DIR", "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN not set. init_data verification will be skipped (dev/test).")

try:
    tg_bot = Bot(token=BOT_TOKEN) if BOT_TOKEN and Bot else None
except Exception as e:
    logger.warning("Failed to init telegram Bot client: %s", e)
    tg_bot = None

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
    con.commit(); con.close()
    logger.info("DB migrated")

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

def extract_user_from_init(init_data: str):
    parsed = verify_init_data(init_data, BOT_TOKEN)
    user = parsed.get("user", {})
    if not user:
        raise ValueError("User data missing in init_data")
    uid = int(user.get("id") or 0)
    return user, uid

def extract_channel_username(link: str) -> str:
    return link.rstrip('/').split('/')[-1]

def is_member_of_channel(user_id: int) -> bool:
    if not tg_bot:
        return False
    channel = extract_channel_username(CHANNEL_LINK)
    if not channel:
        return False
    chat_id = channel if channel.startswith('@') else f"@{channel}"
    try:
        member = tg_bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.debug("Channel membership check error: %s", e)
        return False

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

@app.post("/webapp/check_join")
async def webapp_check_join(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    member = is_member_of_channel(uid)
    return {"ok": True, "member": member, "channel": CHANNEL_LINK}

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
    cur.execute("SELECT COALESCE(coins,0), COALESCE(ads_watched,0), boost_until FROM users WHERE user_id = ?", (uid,))
    row = cur.fetchone(); con.close()
    if not row:
        return {"ok": True, "coins": 0, "ads_watched": 0}
    coins, ads_watched, boost_until = row
    return {"ok": True, "coins": coins, "ads_watched": ads_watched, "boost_until": boost_until}

@app.get("/webapp/get_tasks")
async def get_tasks(page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=50)):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM tasks")
    total = cur.fetchone()[0]
    offset = (page - 1) * per_page
    cur.execute("SELECT task_id, title, description, link, reward FROM tasks ORDER BY task_id DESC LIMIT ? OFFSET ?", (per_page, offset))
    rows = cur.fetchall(); con.close()
    tasks = [{"task_id": r[0], "title": r[1], "description": r[2], "link": r[3], "reward": r[4]} for r in rows]
    return {"ok": True, "tasks": tasks, "page": page, "per_page": per_page, "total": total}

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
        return JSONResponse({"ok": False, "error": "join_channel", "channel": CHANNEL_LINK})
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
    ts = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    ext = pathlib.Path(file.filename).suffix or ".jpg"
    fname = f"{uid}_{ts}{ext}"
    fpath = os.path.join(UPLOAD_DIR, fname)
    content = await file.read()
    with open(fpath, "wb") as f:
        f.write(content)
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (uid, user.get("username") or user.get("first_name") or f"user{uid}"))
    cur.execute("INSERT INTO task_submissions (user_id, task_id, file_path, submitted_at) VALUES (?, ?, ?, ?)", (uid, task_id, fpath, datetime.datetime.utcnow().isoformat()))
    con.commit(); con.close()
    return {"ok": True, "msg": "Proof submitted (image). Waiting for review."}

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
        subs.append({
            "submission_id": r["submission_id"],
            "task_id": r["task_id"],
            "file_path": r["file_path"],
            "status": r["status"],
            "submitted_at": r["submitted_at"],
            "reviewed_by": r["reviewed_by"],
            "review_reason": r["review_reason"]
        })
    return {"ok": True, "submissions": subs}

@app.post("/webapp/list_submissions")
async def list_submissions(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT submission_id, user_id, task_id, file_path, status, submitted_at FROM task_submissions ORDER BY submission_id DESC LIMIT 200")
    rows = cur.fetchall(); con.close()
    out = []
    for r in rows:
        out.append({"submission_id": r["submission_id"], "user_id": r["user_id"], "task_id": r["task_id"], "file_path": r["file_path"], "status": r["status"], "submitted_at": r["submitted_at"]})
    return {"ok": True, "submissions": out}

@app.post("/webapp/review_submission")
async def review_submission(payload: dict):
    init_data = payload.get("init_data")
    submission_id = payload.get("submission_id")
    action = payload.get("action")
    reason = payload.get("reason", "")
    if not init_data or submission_id is None or not action:
        raise HTTPException(status_code=400, detail="init_data, submission_id, and action required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT submission_id, user_id, task_id, status FROM task_submissions WHERE submission_id = ?", (submission_id,))
    row = cur.fetchone()
    if not row:
        con.close()
        raise HTTPException(status_code=404, detail="submission_not_found")
    if row["status"] != "pending":
        con.close()
        return {"ok": False, "msg": "submission_already_reviewed"}
    target_user = row["user_id"]
    task_id = row["task_id"]
    if action == "approve":
        cur.execute("SELECT reward FROM tasks WHERE task_id = ?", (task_id,))
        tre = cur.fetchone()
        reward = tre["reward"] if tre else 0
        cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins) VALUES (?, ?, 0)", (target_user, f"user{target_user}"))
        cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ? WHERE user_id = ?", (reward, target_user))
        cur.execute("UPDATE task_submissions SET status = 'approved', reviewed_by = ?, review_reason = ? WHERE submission_id = ?", (uid, reason, submission_id))
        con.commit(); con.close()
        return {"ok": True, "msg": "approved", "reward": reward}
    elif action == "reject":
        cur.execute("UPDATE task_submissions SET status = 'rejected', reviewed_by = ?, review_reason = ? WHERE submission_id = ?", (uid, reason, submission_id))
        con.commit(); con.close()
        return {"ok": True, "msg": "rejected"}
    else:
        con.close()
        raise HTTPException(status_code=400, detail="invalid_action")

@app.post("/webapp/add_task")
async def add_task(payload: dict):
    init_data = payload.get("init_data")
    title = payload.get("title")
    description = payload.get("description", "")
    link = payload.get("link", "")
    reward = int(payload.get("reward") or 0)
    if not init_data or not title:
        raise HTTPException(status_code=400, detail="init_data and title required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT INTO tasks (title, description, link, reward) VALUES (?, ?, ?, ?)", (title, description, link, reward))
    con.commit(); con.close()
    return {"ok": True, "msg": "task_added"}

@app.post("/webapp/delete_task")
async def delete_task(payload: dict):
    init_data = payload.get("init_data")
    task_id = payload.get("task_id")
    if not init_data or not task_id:
        raise HTTPException(status_code=400, detail="init_data and task_id required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    cur.execute("DELETE FROM task_submissions WHERE task_id = ?", (task_id,))
    con.commit(); con.close()
    return {"ok": True, "msg": "task_deleted"}

@app.get("/webapp/leaderboard")
async def leaderboard(page: int = Query(1, ge=1), per_page: int = Query(20, ge=1, le=100)):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    offset = (page - 1) * per_page
    cur.execute("SELECT user_id, username, coins FROM users ORDER BY coins DESC LIMIT ? OFFSET ?", (per_page, offset))
    rows = cur.fetchall(); con.close()
    lb = [{"user_id": r["user_id"], "username": r["username"], "coins": r["coins"]} for r in rows]
    return {"ok": True, "leaderboard": lb, "page": page, "per_page": per_page, "total": total}

@app.post("/webapp/daily_claim")
async def daily_claim(payload: dict):
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
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins) VALUES (?, ?, 0)", (uid, user.get("username") or user.get("first_name") or f"user{uid}"))
    cur.execute("SELECT last_daily FROM users WHERE user_id = ?", (uid,))
    last = cur.fetchone()["last_daily"]
    today = datetime.datetime.utcnow().date().isoformat()
    if last == today:
        con.close()
        return {"ok": False, "msg": "already_claimed"}
    daily_reward = 50
    cur.execute("UPDATE users SET coins = COALESCE(coins,0) + ?, last_daily = ? WHERE user_id = ?", (daily_reward, today, uid))
    con.commit(); cur.execute("SELECT coins FROM users WHERE user_id = ?", (uid,)); coins = cur.fetchone()["coins"]
    con.close()
    return {"ok": True, "coins_awarded": daily_reward, "coins_total": coins}

@app.post("/webapp/add_verifier")
async def add_verifier(payload: dict):
    init_data = payload.get("init_data")
    verifier_id = payload.get("verifier_id")
    if not init_data or verifier_id is None:
        raise HTTPException(status_code=400, detail="init_data and verifier_id required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO verifiers (verifier_id) VALUES (?)", (verifier_id,))
    con.commit(); con.close()
    return {"ok": True, "msg": "verifier_added"}

@app.post("/webapp/remove_verifier")
async def remove_verifier(payload: dict):
    init_data = payload.get("init_data")
    verifier_id = payload.get("verifier_id")
    if not init_data or verifier_id is None:
        raise HTTPException(status_code=400, detail="init_data and verifier_id required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    user = parsed.get("user", {})
    uid = int(user.get("id") or 0)
    if uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="admin_only")
    con = get_db(); cur = con.cursor()
    cur.execute("DELETE FROM verifiers WHERE verifier_id = ?", (verifier_id,))
    con.commit(); con.close()
    return {"ok": True, "msg": "verifier_removed"}

@app.get("/webapp/get_verifiers")
async def get_verifiers():
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT verifier_id FROM verifiers")
    rows = cur.fetchall(); con.close()
    return {"ok": True, "verifiers": [r["verifier_id"] for r in rows]}

@app.get("/balance/{user_id}")
async def balance(user_id: int):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT COALESCE(coins,0), COALESCE(ads_watched,0), boost_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone(); con.close()
    if not row:
        return {"ok": True, "coins": 0, "ads_watched": 0, "boost_active": False}
    coins, ads_watched, boost_until = row
    boost_active = False
    if boost_until:
        try:
            until = datetime.datetime.fromisoformat(boost_until.replace("Z", ""))
            boost_active = until > datetime.datetime.utcnow()
        except:
            boost_active = False
    return {"ok": True, "coins": coins, "ads_watched": ads_watched, "boost_active": boost_active}

async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    args = context.args
    referrer_id = int(args[0]) if args and args[0].isdigit() else None
    con = get_db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, coins, joined_at) VALUES (?, ?, 100, ?)", (u.id, u.username or u.first_name or f"user{u.id}", datetime.datetime.utcnow().isoformat()))
    if referrer_id and referrer_id != u.id:
        try:
            cur.execute("INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?, ?)", (referrer_id, u.id))
            cur.execute("UPDATE users SET coins = coins + 200 WHERE user_id = ?", (referrer_id,))
        except Exception as e:
            logger.warning("referral error: %s", e)
    con.commit(); con.close()
    try:
        kb = [[InlineKeyboardButton("Open Mini App", web_app=WebAppInfo(url=WEBAPP_URL))]] if WEBAPP_URL else []
        reply_markup = InlineKeyboardMarkup(kb) if kb else None
        if reply_markup:
            await context.bot.send_message(chat_id=u.id, text="Open Mini App", reply_markup=reply_markup)
    except Exception as e:
        logger.debug("Could not send WebApp button: %s", e)

async def bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Callback received.")

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
    asyncio.run(_main())

if __name__ == "__main__":
    migrate()
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    logger.info(f"Starting HTTP server on port {PORT} ...")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
