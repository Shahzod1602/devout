"""Centralised configuration: file paths, secrets, model names, timeouts.

Secret'lar `.env` faylidan o'qiladi (python-dotenv). Local dev uchun loyiha
root'da `.env` bo'lishi kerak; Docker'da `--env-file` orqali uzatiladi.
Template uchun `.env.example` ga qarang.
"""
import os
import ssl
from pathlib import Path

import certifi
from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name!r} is required but not set. "
            f"Check your .env file or container env config."
        )
    return value


# === File paths ===
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot_data.db"
TOKEN_CACHE_FILE = DATA_DIR / "groups_token_cache.json"
STARTED_GROUPS_FILE = DATA_DIR / "started_groups.json"

# === SSL ===
ssl_context = ssl.create_default_context(cafile=certifi.where())

# === Secrets / API keys ===
BOT_TOKEN = _require_env("BOT_TOKEN")
OPENAI_API_KEY = _require_env("OPENAI_API_KEY")
GEMINI_API_KEY = _require_env("GEMINI_API_KEY")
GROQ_API_KEY = _require_env("GROQ_API_KEY")

# === Gemini model ===
GEMINI_BOT_MODEL = "gemini-2.5-flash"

# === Error logger Telegram bot ===
ERROR_BOT_TOKEN = _require_env("ERROR_BOT_TOKEN")
ERROR_GROUP_ID = int(os.environ.get("ERROR_GROUP_ID", "-5133077215"))

# === Environment ===
# ENV_LABEL — error log message'ida ko'rinadi: "❌ [bot] ..." yoki "❌ [botprod] ...".
# Test va prod fork qilinmagan — bir xil kod, faqat env farq qiladi.
ENV_LABEL = os.environ.get("ENV_LABEL", "bot")

# BOT_PORT — uvicorn (FastAPI + /check-bol) port'i.
# Test: 8045, prod: 8090. Dockerfile EXPOSE ham shu env'dan o'qiydi.
BOT_PORT = int(os.environ.get("BOT_PORT", "8045"))

# === External API URLs ===
# BASE_URL — backend domeni. Test: api.abstract-it.uz/api, prod: api.prod.abstract-it.uz/api.
BASE_URL = os.environ.get("BASE_URL", "https://api.abstract-it.uz/api").rstrip("/")
LOGIN_URL = f"{BASE_URL}/auth/login"
SWAGGER_URL = f"{BASE_URL}/tickets"
VALIDATE_TOKEN_URL = f"{BASE_URL}/general-settings/validate-bot-token"
INTERNAL_VALIDATE_TOKEN_URL = f"{BASE_URL}/general-settings/validate-bot-token/internal"
ACTION_LOGS_URL = f"{BASE_URL}/action-logs"
CHECKIN_CHECKOUT_URL = f"{BASE_URL}/stops/checkin-checkout"

# SLEEP_TIMER_PATH — test'da `/v1/drivers/sleep-timer/by-group`,
# prod'da `/drivers/sleep-timer/by-group` (prod backend `/v1` segmentini
# qabul qilmaydi). Path BASE_URL'dan keyin keladi, leading "/" bilan.
SLEEP_TIMER_PATH = os.environ.get("SLEEP_TIMER_PATH", "/v1/drivers/sleep-timer/by-group")
if not SLEEP_TIMER_PATH.startswith("/"):
    SLEEP_TIMER_PATH = "/" + SLEEP_TIMER_PATH
SLEEP_TIMER_URL = f"{BASE_URL}{SLEEP_TIMER_PATH}"

# AskAI — hozircha test va prod ikkalasida `askai.test.abstract-it.uz` ishlatilmoqda.
VERIFY_DELIVERY_URL = os.environ.get(
    "VERIFY_DELIVERY_URL",
    "https://askai.test.abstract-it.uz/verify-delivery",
)

# === API login credentials ===
LOGIN_CREDENTIALS = {
    "email": _require_env("LOGIN_EMAIL"),
    "password": _require_env("LOGIN_PASSWORD"),
}

# === Default quick buttons (Telegram UI) ===
DEFAULT_QUICK_BUTTONS = [
    "🚛 Vehicle Issue",
    "📦 Delivery Problem",
    "🔄 System Issue",
    "⚠️ Safety Concern",
    "💰 Payment Issue",
    "📋 HR Question",
]

# === Domain mappings ===
DEPARTMENT_MAP = {"fleet": 0, "updater": 1, "dispatcher": 2, "insurance": 3, "safety": 4, "hr": 5, "accounting": 6}
PRIORITY_MAP = {"high": 0, "medium": 1, "low": 2}
STATUS_TODO = 0

TIMEZONE_MAP = {"est": 0, "edt": 0, "cst": 1, "cdt": 1, "mst": 2, "mdt": 2, "pst": 3, "pdt": 3}
TIMEZONE_UTC_OFFSET = {"est": -5, "edt": -4, "cst": -6, "cdt": -5, "mst": -7, "mdt": -6, "pst": -8, "pdt": -7}

# === Timeouts / cooldowns ===
COOLDOWN_DURATION = 30        # seconds — driver ticket cooldown
CONVERSATION_TIMEOUT = 60     # seconds — non-driver conversation window

# === Feature flags ===
GROUP_TICKET_NOTIFICATIONS = True  # Ticket yuborilsa groupga ham xabar chiqadi
