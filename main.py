# main.py
import logging, sqlite3, time, os, random, string
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

import config

# ------- Logging -------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xrewardbot")

# ------- Bot init -------
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(bot)

# In-memory pending submission tracker
pending_submission: Dict[int, int] = {}

# ------- DB helpers -------
def get_conn():
    conn = sqlite3.connect(config.DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    c = get_conn()
    cur = c.cursor()
    # users: store referral_code, ads_watched, boost_expiry, boost_count
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id INTEGER PRIMARY KEY,
      username TEXT,
      referral_code TEXT UNIQUE,
      inviter_code TEXT,
      coins INTEGER DEFAULT 0,
      referrals INTEGER DEFAULT 0,
      ads_watched INTEGER DEFAULT 0,
      boost_count INTEGER DEFAULT 0,
      boost_expiry INTEGER DEFAULT 0,
      last_daily INTEGER DEFAULT 0,
      last_ad_claim_at INTEGER DEFAULT 0,
      created_at INTEGER DEFAULT (strftime('%s','now'))
    );
    """)
    # tasks
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      task_id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT,
      description TEXT,
      link TEXT,
      reward INTEGER,
      task_type TEXT,
      active INTEGER DEFAULT 1,
      created_at INTEGER DEFAULT (strftime('%s','now'))
    );
    """)
    # submissions
    cur.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
      submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id INTEGER,
      user_id INTEGER,
      file_id TEXT,
      file_type TEXT,
      caption TEXT,
      status TEXT DEFAULT 'pending',
      verifier_id INTEGER,
      reason TEXT,
      created_at INTEGER DEFAULT (strftime('%s','now')),
      verified_at INTEGER
    );
    """)
    c.commit()
    c.close()

init_db()

# ------- Utility functions -------
def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS

def is_verifier(uid: int) -> bool:
    return uid in config.VERIFIER_IDS or is_admin(uid)

def unique_referral_code():
    # format: XRB + 6 uppercase digits
    conn = get_conn()
    cur = conn.cursor()
    while True:
        code = "XRB" + "".join(random.choices(string.digits, k=6))
        cur.execute("SELECT 1 FROM users WHERE referral_code = ?", (code,))
        if not cur.fetchone():
            conn.close()
            return code

def ensure_user(user_id: int, username: Optional[str] = ""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, referral_code) VALUES (?,?,?)",
                (user_id, username or "", unique_referral_code()))
    conn.commit()
    conn.close()

def get_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_by_code(code: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE referral_code = ?", (code,))
    r = cur.fetchone()
    conn.close()
    return r

def add_coins(user_id: int, amount: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    cur.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
    new = cur.fetchone()["coins"]
    conn.close()
    return new

def set_boost_expiry(user_id: int, expiry_ts: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET boost_expiry = ?, boost_count = boost_count + 1 WHERE user_id = ?", (expiry_ts, user_id))
    conn.commit()
    conn.close()

def add_ad_watch(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET ads_watched = ads_watched + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    cur.execute("SELECT ads_watched FROM users WHERE user_id = ?", (user_id,))
    val = cur.fetchone()["ads_watched"]
    conn.close()
    return val

def create_task(title, desc, link, reward, task_type):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO tasks (title, description, link, reward, task_type) VALUES (?,?,?,?,?)",
                (title, desc, link, reward, task_type))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid

def list_active_tasks():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE active=1 ORDER BY task_id")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_task(task_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    r = cur.fetchone()
    conn.close()
    return r

def create_submission(task_id, user_id, file_id, file_type, caption):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO submissions (task_id, user_id, file_id, file_type, caption) VALUES (?,?,?,?,?)",
                (task_id, user_id, file_id, file_type, caption))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid

def list_pending_submissions(limit=50):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM submissions WHERE status='pending' ORDER BY created_at ASC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def approve_submission_db(sid, verifier_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM submissions WHERE submission_id = ?", (sid,))
    s = cur.fetchone()
    if not s or s["status"] != "pending":
        conn.close()
        return False, "not found or not pending"
    task = get_task(s["task_id"])
    reward = task["reward"] if task else 0
    cur.execute("UPDATE submissions SET status='approved', verifier_id=?, verified_at=? WHERE submission_id=?",
                (verifier_id, int(time.time()), sid))
    cur.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (reward, s["user_id"]))
    conn.commit()
    conn.close()
    return True, reward

def reject_submission_db(sid, verifier_id, reason):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM submissions WHERE submission_id = ?", (sid,))
    s = cur.fetchone()
    if not s or s["status"] != "pending":
        conn.close()
        return False, "not found or not pending"
    cur.execute("UPDATE submissions SET status='rejected', verifier_id=?, reason=?, verified_at=? WHERE submission_id=?",
                (verifier_id, reason, int(time.time()), sid))
    conn.commit()
    conn.close()
    return True, None

def register_referral_code(user_id, code):
    # code is inviter's referral_code
    inviter = get_user_by_code(code)
    if not inviter:
        return False, "Invalid code"
    if inviter["user_id"] == user_id:
        return False, "Cannot refer yourself"
    u = get_user(user_id)
    if u and u["inviter_code"]:
        return False, "Already referred"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET inviter_code=?, referrals = referrals + 1, coins = coins + ? WHERE user_id=?",
                (code, config.REWARDS["referral"], inviter["user_id"]))
    # credit referrer coins too
    cur.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (config.REWARDS["referral"], inviter["user_id"]))
    conn.commit()
    # fetch new inviter balance
    cur.execute("SELECT coins FROM users WHERE user_id = ?", (inviter["user_id"],))
    new_balance = cur.fetchone()["coins"]
    conn.close()
    return True, new_balance

def get_leaderboards(limit=10):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, coins FROM users ORDER BY coins DESC LIMIT ?", (limit,))
    coins = cur.fetchall()
    cur.execute("SELECT user_id, username, referrals FROM users ORDER BY referrals DESC LIMIT ?", (limit,))
    refs = cur.fetchall()
    cur.execute("SELECT user_id, username, ads_watched FROM users ORDER BY ads_watched DESC LIMIT ?", (limit,))
    ads = cur.fetchall()
    cur.execute("SELECT user_id, username, boost_count FROM users ORDER BY boost_count DESC LIMIT ?", (limit,))
    boosts = cur.fetchall()
    conn.close()
    return {"coins": coins, "refs": refs, "ads": ads, "boosts": boosts}

# ------- UI helpers -------
def main_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🎯 Tasks", callback_data="menu_tasks"),
        InlineKeyboardButton("💰 Coins", callback_data="menu_coins"),
    )
    kb.add(
        InlineKeyboardButton("📺 Watch Ad (Get +100)", callback_data="menu_watch_ad"),
        InlineKeyboardButton("🔥 Boost Mode", callback_data="menu_boost")
    )
    kb.add(
        InlineKeyboardButton("🤝 Refer", callback_data="menu_refer"),
        InlineKeyboardButton("🏆 Leaderboards", callback_data="menu_leaderboard")
    )
    if config.MINIAPP_URL:
        kb.add(InlineKeyboardButton("🎮 Open Mini App", web_app=WebAppInfo(url=config.MINIAPP_URL)))
    return kb

def back_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ Back", callback_data="menu_back"))
    return kb

# ------- Handlers -------
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    args = message.get_args().strip()
    uid = message.from_user.id
    uname = message.from_user.username or message.from_user.full_name
    ensure_user(uid, uname)

    # if args look like referral code
    if args:
        # args might be referral code like XRB123456
        ok, info = register_referral_code(uid, args) if args.startswith("XRB") else (False, None)
        if ok:
            await message.answer(f"🎉 Referral registered! Referrer new balance: {info} coins.")
        else:
            # if register_referral_code returned False with reason, inform user
            if isinstance(info, str):
                await message.answer(f"Referral not recorded: {info}")
    await message.answer(f"Welcome {uname}! Use the buttons below to start.", reply_markup=main_menu_kb())

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("menu_"))
async def cb_menu(q: types.CallbackQuery):
    cmd = q.data.split("_",1)[1]
    uid = q.from_user.id

    if cmd == "tasks":
        tasks = list_active_tasks()
        if not tasks:
            await q.message.answer("No active tasks right now. Admin can add tasks with /addtask")
            await q.answer()
            return
        for t in tasks:
            title = t["title"]
            desc = t["description"]
            reward = t["reward"]
            tid = t["task_id"]
            text = f"🔹 <b>{title}</b>\n{desc}\nReward: <b>{reward} coins</b>\nTask ID: {tid}"
            kb = InlineKeyboardMarkup(row_width=2)
            if t["link"]:
                kb.add(InlineKeyboardButton("Open Link", url=t["link"]))
            kb.add(InlineKeyboardButton("✅ Submit Proof", callback_data=f"task_submit:{tid}"),
                   InlineKeyboardButton("⬅️ Back", callback_data="menu_back"))
            await q.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await q.answer()
        return

    if cmd == "coins":
        u = get_user(uid)
        coins = u["coins"] if u else 0
        ref_link = f"https://t.me/{(await bot.get_me()).username}?start={u['referral_code'] if u else ''}"
        text = f"💰 <b>Your Balance:</b> {coins} coins\n\n🔗 <b>Your referral code:</b> `{u['referral_code']}`\nShare this code with friends.\n\n📥 Referral link:\n{ref_link}"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("⬅️ Back", callback_data="menu_back"))
        await q.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await q.answer()
        return

    if cmd == "watch_ad":
        # show ad image/gif if configured then show buttons
        if config.IMG_AD:
            await q.message.answer_photo(config.IMG_AD, caption="Watch this short ad and earn +100 coins!", reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("✅ I Watched (Claim +100)", callback_data="ad_claim"),
                InlineKeyboardButton("⬅️ Back", callback_data="menu_back")
            ))
        else:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("✅ I Watched (Claim +100)", callback_data="ad_claim"))
            kb.add(InlineKeyboardButton("⬅️ Back", callback_data="menu_back"))
            await q.message.answer("📺 Watch Ad — Click below when you've finished watching to claim +100 coins.", reply_markup=kb)
        await q.answer()
        return

    if cmd == "boost":
        # show boost info and options: Activate using ad watch (consume 1 ad watch if available) or watch ad now for coins and possibly auto-boost
        text = ("🔥 <b>Boost Mode</b>\n\nActivate Boost to earn DOUBLE coins for all tasks for 1 hour.\n\n"
                "Options:\n1) Watch an ad now: +100 coins (and every 5th ad auto-activates Boost for 1 hour)\n"
                "2) If you already have ad watches, use 1 ad to manually activate Boost for 1 hour.")
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("📺 Watch Ad (+100)", callback_data="menu_watch_ad"),
               InlineKeyboardButton("🚀 Activate Boost (use 1 ad)", callback_data="activate_boost"))
        kb.add(InlineKeyboardButton("⬅️ Back", callback_data="menu_back"))
        await q.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await q.answer()
        return

    if cmd == "refer":
        u = get_user(uid)
        ref_code = u["referral_code"] if u else ""
        text = f"🤝 <b>Your Referral Code</b>\nShare this with friends. When they use it in /start or in their bot start args you both get rewards.\n\nYour code: `{ref_code}`\nReward per successful refer: {config.REWARDS['referral']} coins"
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Back", callback_data="menu_back"))
        await q.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await q.answer()
        return

    if cmd == "leaderboard":
        boards = get_leaderboards(10)
        text = "🏆 <b>Leaderboards</b>\n\nSelect which leaderboard to view:"
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(InlineKeyboardButton("💎 Coins", callback_data="lb_coins"),
               InlineKeyboardButton("👥 Referrals", callback_data="lb_refs"))
        kb.add(InlineKeyboardButton("📺 Ads Watched", callback_data="lb_ads"),
               InlineKeyboardButton("🚀 Boosts", callback_data="lb_boosts"))
        kb.add(InlineKeyboardButton("⬅️ Back", callback_data="menu_back"))
        await q.message.answer(text, parse_mode="HTML", reply_markup=kb)
        await q.answer()
        return

    if cmd == "back":
        await q.message.answer("Main Menu:", reply_markup=main_menu_kb())
        await q.answer()
        return

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("task_submit:"))
async def cb_task_submit(q: types.CallbackQuery):
    uid = q.from_user.id
    try:
        tid = int(q.data.split(":",1)[1])
    except:
        await q.answer("Invalid task id", show_alert=True)
        return
    pending_submission[uid] = tid
    await q.message.answer(f"📸 Send proof (photo/video/document) now for Task ID {tid}. Use /cancel to stop.")
    await q.answer()

@dp.message_handler(commands=["cancel"])
async def cmd_cancel(m: types.Message):
    uid = m.from_user.id
    if uid in pending_submission:
        pending_submission.pop(uid, None)
        await m.reply("Cancelled.")
    else:
        await m.reply("Nothing to cancel.")

@dp.message_handler(content_types=["photo","video","document"])
async def handle_proof(m: types.Message):
    uid = m.from_user.id
    if uid not in pending_submission:
        await m.reply("To submit proof, click 'Submit Proof' under a task or use /submit <task_id>.")
        return
    tid = pending_submission.pop(uid)
    file_id = None
    ftype = m.content_type
    if ftype == "photo":
        file_id = m.photo[-1].file_id
    elif ftype == "video":
        file_id = m.video.file_id
    else:
        file_id = m.document.file_id
    caption = m.caption or ""
    sid = create_submission(tid, uid, file_id, ftype, caption)
    await m.reply(f"✅ Submission received (ID: {sid}). Verifiers will review it soon.")
    # notify verifiers/admins
    text = f"🆕 New submission #{sid}\nUser: <a href='tg://user?id={uid}'>{uid}</a>\nTask: {tid}\nCaption: {caption}"
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_sub:{sid}"),
        InlineKeyboardButton("❌ Reject (use /reject)", callback_data=f"reject_req:{sid}")
    )
    targets = set(config.VERIFIER_IDS + config.ADMIN_IDS)
    for admin in targets:
        try:
            if ftype == "photo":
                await bot.send_photo(admin, file_id, caption=text, parse_mode="HTML", reply_markup=kb)
            elif ftype == "video":
                await bot.send_video(admin, file_id, caption=text, parse_mode="HTML", reply_markup=kb)
            else:
                await bot.send_document(admin, file_id, caption=text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            log.exception("notify failed: %s", e)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("approve_sub:"))
async def cb_approve_sub(q: types.CallbackQuery):
    uid = q.from_user.id
    if not is_verifier(uid):
        await q.answer("⛔ Not allowed", show_alert=True); return
    sid = int(q.data.split(":",1)[1])
    ok, result = approve_submission_db(sid, uid)
    if not ok:
        await q.answer(f"Error: {result}", show_alert=True); return
    reward = result
    sub = None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM submissions WHERE submission_id = ?", (sid,))
    sub = cur.fetchone(); conn.close()
    try:
        await bot.send_message(sub["user_id"], f"✅ Submission #{sid} approved. You earned +{reward} coins.")
    except: pass
    await q.message.edit_caption((q.message.caption or "") + f"\n\n✅ Approved by {uid} — +{reward} coins")
    await q.answer("Approved.")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("reject_req:"))
async def cb_reject_req(q: types.CallbackQuery):
    uid = q.from_user.id
    if not is_verifier(uid):
        await q.answer("⛔ Not allowed", show_alert=True); return
    sid = int(q.data.split(":",1)[1])
    await q.answer("To reject, use /reject <submission_id> <reason>", show_alert=True)

@dp.message_handler(commands=["reject"])
async def cmd_reject(m: types.Message):
    if not is_verifier(m.from_user.id):
        return await m.reply("⛔ Not allowed")
    parts = m.get_args()
    if not parts:
        return await m.reply("Usage: /reject <submission_id> <reason>")
    pid = parts.split(" ",1)
    try:
        sid = int(pid[0])
    except:
        return await m.reply("Invalid submission id")
    reason = pid[1] if len(pid) > 1 else "Rejected"
    ok, err = reject_submission_db(sid, m.from_user.id, reason)
    if not ok:
        return await m.reply(f"Error: {err}")
    # notify submitter
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id FROM submissions WHERE submission_id = ?", (sid,))
    r = cur.fetchone(); conn.close()
    if r:
        try:
            await bot.send_message(r["user_id"], f"❌ Submission #{sid} rejected.\nReason: {reason}")
        except: pass
    await m.reply(f"Submission {sid} rejected.")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("ad_claim"))
async def cb_ad_claim(q: types.CallbackQuery):
    uid = q.from_user.id
    u = get_
