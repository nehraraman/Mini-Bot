#!/usr/bin/env python3
import os
import sqlite3
import hmac
import hashlib
import json
import datetime
import pathlib
import asyncio

from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

#───────────────────────────────────────────────────────────────────────────────
# CONFIG & SETUP
#───────────────────────────────────────────────────────────────────────────────

# load env vars if you use python-dotenv locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip() or "X_Reward_Bot"
ADMIN_IDS    = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
WEBAPP_URL   = os.getenv("WEBAPP_URL", "").rstrip("/")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/X_Reward_botChannel")
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/xrewardchannel")
PORT         = int(os.getenv("PORT", "8080"))

DATA_DIR  = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH   = os.path.join(DATA_DIR, os.getenv("DB_PATH", "bot.db"))
UPLOAD_DIR = os.path.join(DATA_DIR, os.getenv("UPLOAD_DIR", "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN not set → Telegram features disabled")

tg_bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

#───────────────────────────────────────────────────────────────────────────────
# DATABASE UTILS
#───────────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def migrate():
    con = get_db()
    cur = con.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS users (
        user_id     INTEGER PRIMARY KEY,
        username    TEXT,
        coins       INTEGER DEFAULT 0,
        referrer_id INTEGER,
        joined_at   TEXT,
        ads_watched INTEGER DEFAULT 0,
        ad_counter  INTEGER DEFAULT 0,
        boost_until TEXT,
        last_daily  TEXT
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS tasks (
        task_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT,
        description TEXT,
        link        TEXT,
        reward      INTEGER
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS task_submissions (
        submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER,
        task_id       INTEGER,
        file_path     TEXT,
        status        TEXT DEFAULT 'pending',
        submitted_at  TEXT,
        reviewed_by   INTEGER,
        review_reason TEXT
      )
    """)
    cur.execute("CREATE TABLE IF NOT EXISTS verifiers (verifier_id INTEGER PRIMARY KEY)")
    cur.execute("""
      CREATE TABLE IF NOT EXISTS referrals (
        referrer_id INTEGER,
        referred_id INTEGER,
        PRIMARY KEY(referrer_id, referred_id)
      )
    """)
    con.commit()
    con.close()
    logger.info("Database migrated")

#───────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
#───────────────────────────────────────────────────────────────────────────────

def verify_init_data(init_data: str, bot_token: str) -> dict:
    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    if 'hash' not in parsed:
        raise ValueError("Missing hash")
    check_hash = parsed.pop('hash')
    data_check = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    calc_hash  = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    if calc_hash != check_hash:
        raise ValueError("Invalid hash")
    if 'user' in parsed:
        try:
            parsed['user'] = json.loads(parsed['user'])
        except:
            pass
    return parsed

def extract_channel_username(link: str) -> str:
    return link.rstrip("/").split("/")[-1]

def is_member_of_channel(user_id: int) -> bool:
    if not tg_bot:
        return False
    channel = extract_channel_username(CHANNEL_LINK)
    chat_id = channel if channel.startswith("@") else f"@{channel}"
    try:
        member = tg_bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

#───────────────────────────────────────────────────────────────────────────────
# FASTAPI APP + ROUTES
#───────────────────────────────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=True
)
# serve your index.html under /static/index.html
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    idx = pathlib.Path("static/index.html")
    if idx.exists():
        return HTMLResponse(idx.read_text(encoding="utf-8"))
    return HTMLResponse("<h3>Please upload static/index.html</h3>", status_code=404)

@app.get("/uploads/{fname}")
async def serve_upload(fname: str):
    safe = os.path.join(UPLOAD_DIR, os.path.basename(fname))
    if not os.path.exists(safe):
        raise HTTPException(404, "Not found")
    return FileResponse(safe)

@app.post("/webapp/check_join")
async def webapp_check_join(payload: dict):
    init_data = payload.get("init_data")
    if not init_data:
        raise HTTPException(400, "init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(401, str(e))
    uid = int(parsed.get("user", {}).get("id", 0))
    member = is_member_of_channel(uid)
    return {"ok": True, "member": member, "channel": CHANNEL_LINK}

@app.get("/webapp/get_tasks")
async def get_tasks(page: int = Query(1, ge=1), per_page: int = Query(10, ge=1, le=50)):
    con = get_db(); cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM tasks")
    total = cur.fetchone()[0]
    offset = (page - 1) * per_page
    cur.execute(
        "SELECT task_id, title, description, link, reward "
        "FROM tasks ORDER BY task_id DESC LIMIT ? OFFSET ?",
        (per_page, offset)
    )
    rows = cur.fetchall(); con.close()
    tasks = [
        {"task_id": r[0], "title": r[1], "description": r[2], "link": r[3], "reward": r[4]}
        for r in rows
    ]
    return {"ok": True, "tasks": tasks, "page": page, "per_page": per_page, "total": total}

@app.get("/balance/{user_id}")
async def balance(user_id: int):
    con = get_db(); cur = con.cursor()
    cur.execute(
        "SELECT COALESCE(coins,0), COALESCE(ads_watched,0), boost_until "
        "FROM users WHERE user_id = ?",
        (user_id,)
    )
    row = cur.fetchone(); con.close()
    if not row:
        return {"ok": True, "coins": 0, "ads_watched": 0, "boost_active": False}
    coins, ads, boost_until = row
    boost_active = False
    if boost_until:
        try:
            until = datetime.datetime.fromisoformat(boost_until.replace("Z", ""))
            boost_active = until > datetime.datetime.utcnow()
        except:
            pass
    return {"ok": True, "coins": coins, "ads_watched": ads, "boost_active": boost_active}

@app.post("/webapp/ad_watched")
async def ad_watched(req: Request):
    payload    = await req.json()
    init_data  = payload.get("init_data", "")
    if not init_data:
        raise HTTPException(400, "init_data required")
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(401, str(e))
    uid = int(parsed["user"].get("id", 0))
    if not is_member_of_channel(uid):
        return JSONResponse({"ok": False, "error": "join_channel", "channel": CHANNEL_LINK})
    username = parsed["user"].get("username") or parsed["user"].get("first_name") or f"user{uid}"
    con = get_db(); cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, coins, ads_watched, ad_counter) "
        "VALUES (?, ?, 0, 0, 0)",
        (uid, username)
    )
    coins_awarded = 100
    cur.execute(
        "UPDATE users SET coins = coins + ?, ads_watched = ads_watched + 1 WHERE user_id = ?",
        (coins_awarded, uid)
    )
    cur.execute("SELECT ad_counter FROM users WHERE user_id = ?", (uid,))
    ad_ctr = (cur.fetchone()[0] or 0) + 1
    boost_until = None
    if ad_ctr >= 3:
        boost_until = (datetime.datetime.utcnow() + datetime.timedelta(hours=2)).isoformat() + "Z"
        cur.execute(
            "UPDATE users SET boost_until = ?, ad_counter = 0 WHERE user_id = ?",
            (boost_until, uid)
        )
        ads_to_next = 3
    else:
        cur.execute("UPDATE users SET ad_counter = ? WHERE user_id = ?", (ad_ctr, uid))
        ads_to_next = 3 - ad_ctr
    con.commit()
    cur.execute("SELECT coins, ads_watched FROM users WHERE user_id = ?", (uid,))
    coins, ads_watched = cur.fetchone(); con.close()
    return {
        "ok": True,
        "coins_awarded": coins_awarded,
        "coins_total": coins,
        "ads_watched": ads_watched,
        "ads_to_next_boost": ads_to_next,
        "boost_until": boost_until
    }

@app.post("/webapp/submit_proof")
async def submit_proof(
    init_data: str = Form(...),
    task_id:  int   = Form(...),
    file:     UploadFile = File(...)
):
    try:
        parsed = verify_init_data(init_data, BOT_TOKEN)
    except Exception as e:
        raise HTTPException(401, str(e))
    uid = int(parsed["user"].get("id", 0))
    if not is_member_of_channel(uid):
        raise HTTPException(403, "join_channel")
    ts    = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    ext   = pathlib.Path(file.filename).suffix or ".jpg"
    fname = f"{uid}_{ts}{ext}"
    fpath = os.path.join(UPLOAD_DIR, fname)
    content = await file.read()
    with open(fpath, "wb") as buf:
        buf.write(content)
    con = get_db(); cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (uid, parsed["user"].get("username") or parsed["user"].get("first_name") or f"user{uid}")
    )
    cur.execute(
        "INSERT INTO task_submissions (user_id, task_id, file_path, submitted_at) "
        "VALUES (?, ?, ?, ?)",
        (uid, task_id, fpath, datetime.datetime.utcnow().isoformat())
    )
    con.commit(); con.close()
    return {"ok": True, "msg": "Proof submitted. Awaiting review."}

# … include your other endpoints here (submissions, review_submission,
#     add_task, delete_task, add_verifier, remove_verifier, leaderboards,
#     daily_claim) exactly as in your original code …

#───────────────────────────────────────────────────────────────────────────────
# TELEGRAM BOT SETUP
#───────────────────────────────────────────────────────────────────────────────

telegram_app = Application.builder().token(BOT_TOKEN).build()

async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    args = context.args
    ref_id = int(args[0]) if args and args[0].isdigit() else None

    con = get_db(); cur = con.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, coins, joined_at) "
        "VALUES (?, ?, 100, ?)",
        (u.id, u.username or u.first_name or f"user{u.id}", datetime.datetime.utcnow().isoformat())
    )
    if ref_id and ref_id != u.id:
        try:
            cur.execute(
                "INSERT OR IGNORE INTO referrals (referrer_id, referred_id) VALUES (?,?)",
                (ref_id, u.id)
            )
            cur.execute("UPDATE users SET coins = coins + 200 WHERE user_id = ?", (ref_id,))
        except:
            pass
    con.commit(); con.close()

    if not is_member_of_channel(u.id):
        kb = [
            [InlineKeyboardButton("Join Channel", url=CHANNEL_LINK)],
            [InlineKeyboardButton("✅ Check Join", callback_data="check_join")]
        ]
        await update.message.reply_text(
            f"Please join {CHANNEL_LINK} then click 'Check Join'.",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    kb = [
        [InlineKeyboardButton("📋 Tasks", callback_data="tasks"),
         InlineKeyboardButton("💰 Coins", callback_data="coins")],
        [InlineKeyboardButton("🏆 Leaderboards", callback_data="leaderboards"),
         InlineKeyboardButton("📺 Dashboard", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("🎁 Daily", callback_data="daily"),
         InlineKeyboardButton("🚀 Power Mode", callback_data="power_info")],
        [InlineKeyboardButton("💼 Referral", callback_data="refer"),
         InlineKeyboardButton("🛟 Support", url=SUPPORT_LINK)],
    ]
    text = (
        f"Welcome {u.first_name}! 👋\n\n"
        f"Referral: https://t.me/{BOT_USERNAME}?start={u.id}"
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # copy your existing callback logic here: check_join, tasks, coins,
    # leaderboards, daily, power_info, mystery, refer, back, etc.
    query = update.callback_query
    await query.answer()
    # …

telegram_app.add_handler(CommandHandler("start", bot_start))
telegram_app.add_handler(CallbackQueryHandler(bot_callback))

#───────────────────────────────────────────────────────────────────────────────
# LIFECYCLE: MIGRATE & START BOT
#───────────────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    migrate()
    if BOT_TOKEN:
        await telegram_app.initialize()
        asyncio.create_task(telegram_app.start())

@app.on_event("shutdown")
async def on_shutdown():
    if BOT_TOKEN:
        await telegram_app.stop()

#───────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT (for local dev)
#───────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting on port {PORT}")
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
