import telebot
from telebot import types
import sqlite3
import random
import time
from config import BOT_TOKEN, ADMIN_IDS

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ---------------- DATABASE SETUP ----------------
conn = sqlite3.connect("bot.db", check_same_thread=False)
c = conn.cursor()

c.execute("""CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    coins INTEGER DEFAULT 0,
    referrals INTEGER DEFAULT 0,
    boost_active_until INTEGER DEFAULT 0
)""")

c.execute("""CREATE TABLE IF NOT EXISTS ads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link TEXT
)""")

conn.commit()

# ---------------- HELPERS ----------------
def get_user(user_id):
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return c.fetchone()

def add_user(user_id):
    if not get_user(user_id):
        c.execute("INSERT INTO users(user_id, coins) VALUES (?, ?)", (user_id, 0))
        conn.commit()

def update_coins(user_id, amount):
    c.execute("UPDATE users SET coins = coins + ? WHERE user_id=?", (amount, user_id))
    conn.commit()

def boost_active(user_id):
    c.execute("SELECT boost_active_until FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row and row[0] > int(time.time()):
        return True
    return False

def activate_boost(user_id, seconds=3600):
    until = int(time.time()) + seconds
    c.execute("UPDATE users SET boost_active_until=? WHERE user_id=?", (until, user_id))
    conn.commit()

# ---------------- COMMANDS ----------------
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    add_user(user_id)

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("💰 Coins", callback_data="coins"),
        types.InlineKeyboardButton("👥 Refer", callback_data="refer")
    )
    keyboard.row(
        types.InlineKeyboardButton("⚡ Boost", callback_data="boost"),
        types.InlineKeyboardButton("📢 Ads", callback_data="ads")
    )
    keyboard.row(
        types.InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")
    )

    bot.send_message(user_id, "👋 Welcome to <b>X Reward Bot</b>\nChoose an option below:", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    if call.data == "coins":
        user = get_user(user_id)
        coins = user[1] if user else 0
        bot.answer_callback_query(call.id, f"💰 You have {coins} coins")
    elif call.data == "refer":
        ref_code = f"REF{user_id}"
        bot.answer_callback_query(call.id, "Share your referral link")
        bot.send_message(user_id, f"👥 Invite friends using your referral link:\nhttps://t.me/{bot.get_me().username}?start={ref_code}")
    elif call.data == "boost":
        if boost_active(user_id):
            bot.send_message(user_id, "⚡ Your boost is already active!")
        else:
            activate_boost(user_id, 3600)
            bot.send_message(user_id, "⚡ Boost mode activated for 1 hour! Double rewards apply.")
    elif call.data == "ads":
        c.execute("SELECT link FROM ads ORDER BY RANDOM() LIMIT 1")
        ad = c.fetchone()
        if ad:
            update_coins(user_id, 100)
            bot.send_message(user_id, f"📢 Watch this ad & earn 100 coins:\n{ad[0]}")
        else:
            bot.send_message(user_id, "❌ No ads available right now.")
    elif call.data == "leaderboard":
        c.execute("SELECT user_id, coins FROM users ORDER BY coins DESC LIMIT 10")
        rows = c.fetchall()
        text = "🏆 <b>Top 10 Users</b>\n\n"
        for i, row in enumerate(rows, start=1):
            text += f"{i}. User {row[0]} — {row[1]} coins\n"
        bot.send_message(user_id, text)

# ---------------- RUN BOT ----------------
bot.infinity_polling()
