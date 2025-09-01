import os

# REQUIRED - set on Railway / env
BOT_TOKEN = os.getenv("BOT_TOKEN", "REPLACE_WITH_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
VERIFIER_IDS = [int(x) for x in os.getenv("VERIFIER_IDS", "").split(",") if x.strip().isdigit()]

# Mini app (Vercel) url
MINIAPP_URL = os.getenv("MINIAPP_URL", "")

# DB filename
DB_NAME = os.getenv("DB_NAME", "database.sqlite3")

# Rewards and settings
REWARDS = {
    "join_follow": int(os.getenv("R_JOIN_FOLLOW", "100")),
    "referral": int(os.getenv("R_REFERRAL", "200")),
    "like_comment_repost": int(os.getenv("R_LIKE_COMMENT", "50")),
    "daily": int(os.getenv("R_DAILY", "50")),
    "ad_watch": int(os.getenv("R_AD_WATCH", "100")),
}
# Cooldowns / durations (seconds)
DAILY_COOLDOWN = int(os.getenv("DAILY_COOLDOWN", 24 * 3600))
AD_COOLDOWN = int(os.getenv("AD_COOLDOWN", 15 * 60))
BOOST_DURATION = int(os.getenv("BOOST_DURATION", 60 * 60))  # 1 hour

# UI image placeholders (optional)
IMG_BOOST = os.getenv("IMG_BOOST", "")       # e.g., https://.../boost.png
IMG_AD = os.getenv("IMG_AD", "")             # e.g., https://.../ad.png
IMG_LEADER = os.getenv("IMG_LEADER", "")     # e.g., https://.../leader.png

# Misc
DB_INIT = True
