import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID")
_OWNER_IDS = {value for value in (_int("OWNER_ID"),) if value}
for _raw_owner in os.getenv("OWNER_IDS", "").split(","):
    try:
        if _raw_owner.strip():
            _OWNER_IDS.add(int(_raw_owner.strip()))
    except ValueError:
        pass
OWNER_IDS = frozenset(_OWNER_IDS)
LOG_CHANNEL = _int("LOG_CHANNEL")
# MAIN_DATA is the one canonical channel whose copies form the user catalog.
MAIN_DATA = _int("MAIN_DATA", _int("STORAGE_CHANNEL", -1003463663979))
# Legacy alias only; new deployments do not need a separate movie channel.
MOVIE_CHANNEL = _int("MOVIE_CHANNEL", MAIN_DATA)
STORAGE_CHANNEL = MAIN_DATA
# ============ MULTI MAIN CHANNEL SUPPORT ============
# Comma-separated list of additional main channels for faster scanning of 100k+ movies.
# Movies are distributed across these channels. Format: "-100xxx,-100yyy,-100zzz"
# MAIN_DATA is always the first channel; these are additional channels.
MAIN_CHANNELS = [MAIN_DATA]
_raw_multi = os.getenv("MAIN_CHANNELS", os.getenv("MOVIE_CHANNELS", "")).strip()
if _raw_multi:
    for _ch in _raw_multi.split(","):
        _ch = _ch.strip()
        if _ch and _ch.lstrip("-").isdigit():
            _cid = int(_ch)
            if _cid not in MAIN_CHANNELS:
                MAIN_CHANNELS.append(_cid)
# =======================================================
SCAN_START_ID = _int("SCAN_START_ID", 200_000)
RECOVERY_WINDOW = max(100, _int("RECOVERY_WINDOW", 5000))
AUTO_SCAN_INTERVAL_SECONDS = max(60, _int("AUTO_SCAN_INTERVAL_SECONDS", 300))
# Resolve the default relative to the project, so a restart from another cwd
# cannot silently open a fresh empty database.
_configured_db_path = os.getenv("DB_PATH", "bot.db")
DB_PATH = str((BASE_DIR / _configured_db_path).resolve()) if not os.path.isabs(_configured_db_path) else _configured_db_path

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
MONGO_URI = os.getenv("MONGO_URI", "").strip()
MONGO_DB = os.getenv("MONGO_DB", "rohan_movies").strip() or "rohan_movies"
TG_API_ID = _int("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "").strip()
ONLINE_PLAYER_BOT = os.getenv("ONLINE_PLAYER_BOT", "@YourOnlinePlayerBot").strip()
# Support group URL shown when no movie is found
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()

DEFAULT_FORCE_CHANNELS = [c.strip() for c in os.getenv("FORCE_CHANNELS", "").split(",") if c.strip()]
DEFAULT_START_PIC = os.getenv("START_PIC_URL", "")
DEFAULT_QR = os.getenv("QR_URL", "")
DEFAULT_UPI = os.getenv("UPI_ID", "")
DEFAULT_CAPTION_JOIN = os.getenv("CAPTION_JOIN", "").strip()
DEFAULT_CAPTION_VISIT = os.getenv("CAPTION_VISIT", "").strip()
DEFAULT_AUTO_DELETE_MINUTES = min(1440, max(0, _int("AUTO_DELETE_MINUTES", 7)))

# Free plan rules
FIRST_DAY_FREE = max(0, _int("FIRST_DAY_FREE", 3))  # pehle din free searches
DAILY_FREE = max(0, _int("DAILY_FREE", 1))        # uske baad roz free searches

# Referral rewards -> hours of premium
REFERRAL_REWARDS = {3: 1, 5: 5, 10: 24}

# (code, label, price, days, emoji, tag, sort)
DEFAULT_PLANS = [
    ("1day", "1 Day", 39, 1, "\U0001F39F\uFE0F", "", 1),
    ("7day", "7 Days", 69, 7, "\U0001F525", "POPULAR", 2),
    ("1month", "1 Month", 149, 30, "\U0001F4C5", "\u2B50 BEST VALUE", 3),
    ("2month", "2 Months", 249, 60, "\U0001F48E", "", 4),
    ("3month", "3 Months", 349, 90, "\U0001F680", "\U0001F525 BEST SELLER", 5),
    ("6month", "6 Months", 499, 180, "\U0001F451", "\U0001F48E BEST DEAL", 6),
    ("1year", "1 Year", 699, 365, "\U0001F3C6", "\U0001F680 SUPER VALUE", 7),
    ("lifetime", "Lifetime", 999, 36500, "\U0001F4A0", "\U0001F451 PREMIUM", 8),
]

# Plan pricing revision. Bump this string to force-reset stored prices to the
# defaults above on the next boot (owner edits after that are preserved).
PLAN_PRICING_REVISION = os.getenv("PLAN_PRICING_REVISION", "v2")

# ============ SCAN SPEED / PERSISTENCE TUNING ============
# Rows buffered before one bulk SQLite write (bigger = faster for 1M+ posts).
SCAN_COMMIT_BATCH = _int("SCAN_COMMIT_BATCH", 5000)
# How often the periodic saver pushes movie metadata to Mongo (seconds).
CATALOG_SAVE_INTERVAL = max(120, _int("CATALOG_SAVE_INTERVAL", 600))
STATE_SAVE_INTERVAL = max(30, _int("STATE_SAVE_INTERVAL", 60))
# One migration attempt window; on timeout it auto-retries from the cursor.
CATALOG_MIGRATION_TIMEOUT = max(300, _int("CATALOG_MIGRATION_TIMEOUT", 1800))
CATALOG_MIGRATION_RETRY_DELAY = max(5, _int("CATALOG_MIGRATION_RETRY_DELAY", 15))
# Heroku SIGKILLs 30s after SIGTERM; stay well inside that window.
SHUTDOWN_SAVE_BUDGET = max(3, _int("SHUTDOWN_SAVE_BUDGET", 8))
# Captions are trimmed before going to Mongo so metadata stays small.
# No media/video is ever stored in Mongo.
MONGO_CAPTION_LIMIT = max(0, _int("MONGO_CAPTION_LIMIT", 200))
# =========================================================

# ============ MULTI-BOT TRANSFER SYSTEM ============
# Helper bot tokens (comma-separated). These bots handle ALL file transfers.
# Main bot NEVER transfers files - only accepts commands.
# Example: "123:AAA,456:BBB,789:CCC"
HELPER_BOT_TOKENS = os.getenv("HELPER_BOT_TOKENS", "")

# Concurrent transfers (higher = faster, Telegram flood limits still apply)
TRANSFER_CONCURRENCY = max(1, _int("TRANSFER_CONCURRENCY", 30))

# Transfer mode: "auto" (forward->copy), "forward" only, "copy" only
TRANSFER_MODE = os.getenv("TRANSFER_MODE", "auto").strip().lower()
if TRANSFER_MODE not in {"auto", "forward", "copy"}:
    TRANSFER_MODE = "auto"
# ===================================================
