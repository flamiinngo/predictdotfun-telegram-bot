# Configuration for Predict.fun Monitor Bot
import os

# ========================================
# YOUR API CREDENTIALS
# ========================================

# ✅ Predict.fun API Key (ALREADY FILLED IN!)
PREDICT_API_KEY = os.environ.get("PREDICT_API_KEY", "67c9c9348416be6715c322bf4ff74b5fd24e")

# ⚠️ YOU NEED TO SET THESE IN RENDER.COM:
# - TELEGRAM_BOT_TOKEN (get from @BotFather)
# - TELEGRAM_CHAT_ID (get from @userinfobot)

# ========================================
# MONITORING SETTINGS
# ========================================

# Alert when bets >= this amount in USDT
WHALE_THRESHOLD = float(os.environ.get("WHALE_THRESHOLD", "0.1"))

# Alert when this many wallets bet the same way within 5 minutes
# NOTE: Actual filter is hardcoded to 5+ for high win rate
MIN_COORDINATED_WALLETS = int(os.environ.get("MIN_COORDINATED_WALLETS", "5"))

# Time window to detect coordination (seconds)
COORDINATION_WINDOW = 300

# Alert on price changes >= this percentage
PRICE_CHANGE_THRESHOLD = 0.05

# How often to check for new activity (seconds)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))

# Leave empty to monitor all markets, or add specific market IDs
MARKETS_TO_MONITOR = []

# ========================================
# TELEGRAM SETTINGS
# ========================================

ENABLE_TELEGRAM = os.environ.get("ENABLE_TELEGRAM", "True").lower() == "true"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ========================================
# DISCORD SETTINGS (Optional)
# ========================================

ENABLE_DISCORD = os.environ.get("ENABLE_DISCORD", "False").lower() == "true"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ========================================
# ADVANCED SETTINGS
# ========================================

# Wallets to track (add addresses here or use /track command in Telegram)
TRACK_SPECIFIC_WALLETS = []

# Database file
DB_PATH = "predict_monitor.db"

# Enable win rate tracking (requires market settlement data)
ENABLE_WIN_RATE_TRACKING = False

# Minimum volume to be considered "sharp bettor"
SHARP_BETTOR_MIN_VOLUME = 1000
