"""Centralised configuration: file paths, secrets, model names, timeouts.

Secret'lar `.env` faylidan o'qiladi (python-dotenv). Local dev uchun loyiha
root'da `.env` bo'lishi kerak; Docker'da `--env-file` orqali uzatiladi.
Template uchun `.env.example` ga qarang.
"""
import json
import logging
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
GROQ_API_KEY = _require_env("GROQ_API_KEY")
# Cerebras — Groq ishlamay qolsa (masalan org billing bloki) fallback. Ixtiyoriy:
# berilmasa fallback o'chadi, faqat Groq ishlatiladi.
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "")

# === Vertex AI (Gemini) ===
# Paperwork tahlili Vertex AI orqali ishlaydi (service account auth), eski
# Gemini Developer API key emas — shuning uchun "API key expired" muammosi yo'q.
# Credential `GOOGLE_APPLICATION_CREDENTIALS` orqali beriladi. Key fayli yo'q
# bo'lsa (CI/test) sokin o'tib ketadi; loyiha None bilan qoladi va haqiqiy
# chaqiruv faqat credential mavjud deploy'da amalga oshadi.
VERTEX_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/shahzod/vertex-keys/vertex-server-key.json",
)
_vertex_key: dict = {}
if VERTEX_CREDENTIALS_PATH and os.path.exists(VERTEX_CREDENTIALS_PATH):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = VERTEX_CREDENTIALS_PATH
    with open(VERTEX_CREDENTIALS_PATH) as _vk:
        _vertex_key = json.load(_vk)

VERTEX_PROJECT = os.getenv(
    "VERTEX_PROJECT",
    _vertex_key.get("project_id") or _vertex_key.get("quota_project_id"),
)
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "global")  # gemini-3.5-flash is only served on "global"

# === Gemini model ===
GEMINI_BOT_MODEL = "gemini-3.5-flash"  # Gemini 3.5 Flash (GA; smarter + faster than 3-flash, better extraction)
# 2026-07-17 fallback: Google global-endpoint flash-sinf sig'im inqirozi (07-15 dan, 3 loyihada
# tasdiqlangan — kvota EMAS, DSQ kontensiya; forum: discuss.ai.google.dev/t/174959). Asosiy model
# 2 marta ketma-ket 429 bersa zaxira regionga o'tamiz — AYNI 3.5-flash, europe-west2
# (London; jonli probe OK — sifat farqi YO'Q, faqat region almashadi).
# Google bo'shagach har yangi so'rov yana asosiydan boshlaydi (yopishqoq holat yo'q).
GEMINI_FALLBACK_ENABLE = os.getenv("GEMINI_FALLBACK_ENABLE", "1").strip() == "1"
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash")
GEMINI_FALLBACK_LOCATION = os.getenv("GEMINI_FALLBACK_LOCATION", "europe-west2")
# Matnli helper'lar (classify/summary/priority, sobiq gpt-4o-mini) — 2026-07-17 OpenAI
# hisobida insufficient_quota (kredit tugagan) sabab Gemini'ga ko'chirildi. A/B 55 keys:
# flash-lite tugallanganlarida 44/44 to'g'ri; narx 4o-mini sinfida. Global-only model.
GEMINI_TEXT_MODEL = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.1-flash-lite")

# === Error logger Telegram bot ===
ERROR_BOT_TOKEN = _require_env("ERROR_BOT_TOKEN")
ERROR_GROUP_ID = int(os.environ.get("ERROR_GROUP_ID", "-5133077215"))

# === Paperwork log group ===
# Har bir paperwork fayli (selected yoki skipped) sababi bilan shu guruhga
# forward qilinadi. 0 bo'lsa — feature o'chiq (group_id berilgunicha).
# 2026-07-07: log-guruh supergroup'ga ko'tarildi — Telegram yangi ID berdi
# (eski -5535325878 endi TelegramMigrateToChat xatosi beradi).
PAPERWORK_LOG_GROUP_ID = int(os.environ.get("PAPERWORK_LOG_GROUP_ID", "-1004283245217"))

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
if "BASE_URL" not in os.environ:
    # EXT-8: prod deploy BASE_URL ni o'rnatishni unutsa, jimgina TEST backend'ga ulanardi.
    logging.getLogger("config").warning(
        "⚠️ BASE_URL env o'rnatilmagan — default (TEST) backend ishlatilmoqda: %s", BASE_URL)
LOGIN_URL = f"{BASE_URL}/auth/login"
SWAGGER_URL = f"{BASE_URL}/tickets"
VALIDATE_TOKEN_URL = f"{BASE_URL}/general-settings/validate-bot-token"
INTERNAL_VALIDATE_TOKEN_URL = f"{BASE_URL}/general-settings/validate-bot-token/internal"
ACTION_LOGS_URL = f"{BASE_URL}/action-logs"
CHECKIN_CHECKOUT_URL = f"{BASE_URL}/stops/checkin-checkout"
PAPERWORK_ISSUES_URL = f"{BASE_URL}/paperwork-issues"

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

# PO ↔ RC reference tekshiruvi: 1 (default) — BOL/POD'dagi PO/ref raqami loadning
# MA'LUM referencelariga (RefNumber + QM poNumber/otherNumber) mos kelishi shart,
# aks holda paperwork kartada ❌. 0 — eski presence-only xulq (kill-switch).
PO_MATCH_ENFORCE = os.environ.get("PO_MATCH_ENFORCE", "1").strip() == "1"

# Sahifa yetishmasa (hujjatda "PAGE: 1 of 3" bosilgan, driver 1 ta yuborgan) BOL/POD
# QABUL QILINMAYDI — driver'dan barcha sahifalar so'raladi. 0 — faqat kartada ❌
# ko'rsatiladi, qabul eski tartibda davom etadi (kill-switch).
PAGE_COUNT_ENFORCE = os.environ.get("PAGE_COUNT_ENFORCE", "1").strip() == "1"

# CLA-887: BOL yuklamasida load-securement (yuk trailerда mahkamlangan) fotosi bo'lmasa
# driver'ga "Load securement pictures were not sent" warning. BloklaMAYDI (page-count kabi
# emas) — faqat ogohlantirish + kartada ❌. 0 — warning o'chadi, kriteriya baribir kartaga
# boradi (backend field qo'shsa) (kill-switch).
LOAD_SECUREMENT_ENFORCE = os.environ.get("LOAD_SECUREMENT_ENFORCE", "1").strip() == "1"

# Trailer drop/pickup hisoboti ("trl # VT700653 / Picked up by: <ISM> / Location / Date /
# Conditon") check-in EMAS: "picked up" CHECKIN_CHECKOUT_KEYWORDS'ga tushib LLM yo'lagiga
# borardi, LLM esa TRAILER raqamini load_id deb qaytarardi (unda 3+ raqam bor —
# is_valid_load_id o'tkazib yuboradi) -> driver'ga soxta "check-in/check-out vaqtlarini
# kiriting" javobi + missing_time statistikasi. 1 (default): matnda LOAD RAQAMI UMUMAN
# yo'q bo'lsa (har bir raqam trailer/seal/truck markeriga yoki sanaga bog'langan —
# checkin.is_trailer_only_reference) xabar check-in oqimidan butunlay chetlab o'tadi
# (javob yo'q, backend yo'q, statistika yo'q). Faqat SHU bitta, strukturaviy mezon:
# shablon-taxminga asoslangan "shape" va LLM'dan keyingi backstop ataylab olib tashlandi
# (haqiqiy check-in'larni jimgina yo'qotardi).
# 0 — eski xulq qaytadi. Kill-switch SHART: over-filtering SOKIN buziladi (driver javob
# olmaydi, /admin'da hech narsa ko'rinmaydi), shuning uchun deploy'siz rollback kerak.
TRAILER_REPORT_SKIP = os.environ.get("TRAILER_REPORT_SKIP", "1").strip() == "1"

# BOL/POD PDF blob'lari (bols/pods.file_blob) load tugagach clear_load_from_cache
# bilan o'chadi, lekin tugamagan/osilib qolgan loadlarniki cheksiz o'sib DB'ni
# shishiradi (audit v3 #19; prod'da 2.7GB kuzatildi). Startup'da saved_at bo'yicha
# shu kundan eski blob'lar tozalanadi (stats-retention naqshi). 0 → TTL o'chiq.
PAPERWORK_BLOB_TTL_DAYS = int(os.environ.get("PAPERWORK_BLOB_TTL_DAYS", "60"))

# === Admin panel ===
# ADMIN_TOKEN — /admin panelining kirish kaliti. O'rnatilmagan bo'lsa panel
# BUTUNLAY O'CHIQ (503) — audit H1 (authsiz control-plane) takrorlanmasin.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()

# === Control-plane auth (audit v3 H2) ===
# Backend webhook endpointlari (api/routes.py: /send-message, /accepted,
# /permissions, /group-deleted, ...) autentifikatsiyasiz edi — tarmoq ichidagi
# istalgan tomon guruhlarga xabar yuborishi / permissions o'zgartirishi mumkin.
# DARK-LAUNCH (backend hali X-Api-Key yubormaydi — u boshqa jamoaniki):
#   CONTROL_PLANE_API_KEY bo'sh        → auth O'CHIQ (legacy, hozirgi xulq).
#   key bor + ENFORCE=0 (default)      → MONITOR: kalitsiz so'rov O'TADI, faqat
#                                        adoption log-summary'si yoziladi.
#   key bor + ENFORCE=1                → kalitsiz/xato so'rov 401.
# Backend kalit yubora boshlaganini MONITOR loglaridan tasdiqlab, keyin
# ENFORCE=1 ga o'tkaziladi (bir env o'zgarishi, kod deploy'siz).
CONTROL_PLANE_API_KEY = os.environ.get("CONTROL_PLANE_API_KEY", "").strip()
CONTROL_PLANE_AUTH_ENFORCE = os.environ.get("CONTROL_PLANE_AUTH_ENFORCE", "0").strip() == "1"

# === Storage / DB backend (docs/POSTGRES_MIGRATION_PLAN.md) ===
# Phase 0: flaglar qo'shildi, DEFAULT = HOZIRGI xulq (hech narsa o'zgarmaydi).
# BLOB_STORAGE_BACKEND: 'db' (inline file_blob — hozirgi) | 's3' (object storage — Phase 1).
# 's3' yoqilганда S3-mos (Cloudflare R2 / Hetzner / MinIO) config'i kerak.
BLOB_STORAGE_BACKEND = os.environ.get("BLOB_STORAGE_BACKEND", "db").strip().lower()
BLOB_S3_ENDPOINT = os.environ.get("BLOB_S3_ENDPOINT", "").strip()
BLOB_S3_BUCKET = os.environ.get("BLOB_S3_BUCKET", "").strip()
BLOB_S3_ACCESS_KEY = os.environ.get("BLOB_S3_ACCESS_KEY", "").strip()
BLOB_S3_SECRET_KEY = os.environ.get("BLOB_S3_SECRET_KEY", "").strip()
BLOB_S3_REGION = os.environ.get("BLOB_S3_REGION", "auto").strip()
# Prefiks — muhitlar (bot/botprod/devbot) bitta bucket'da ajralsin.
BLOB_S3_PREFIX = os.environ.get("BLOB_S3_PREFIX", ENV_LABEL).strip()

# DB_BACKEND: 'sqlite' (hozirgi) | 'postgres' (Phase 2). DATABASE_URL faqat 'postgres'da.
DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite").strip().lower()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# === API login credentials ===
LOGIN_CREDENTIALS = {
    "email": _require_env("LOGIN_EMAIL"),
    "password": _require_env("LOGIN_PASSWORD"),
}

# === Domain mappings ===
# Backend Department enum bilan sinxron (updater-agent Department.cs): ELD = 7 (2026-07-10 qo'shilgan).
DEPARTMENT_MAP = {"fleet": 0, "updater": 1, "dispatcher": 2, "insurance": 3, "safety": 4, "hr": 5, "accounting": 6, "eld": 7}
PRIORITY_MAP = {"high": 0, "medium": 1, "low": 2}
STATUS_TODO = 0

# Timezone mapping checkin.py'da (_TZ_REGION/_REGION_ENUM): abbreviatura → IANA
# region, DST-aware. Eski fixed-offset jadvallar (est=-5...) olib tashlandi —
# ular yozda +1h xato berardi va checkout'ga alohida default qo'llanib
# +2/3h siljish bugini keltirgan edi (2026-07-15).

# === Timeouts / cooldowns ===
COOLDOWN_DURATION = 30        # seconds — driver ticket cooldown
CONVERSATION_TIMEOUT = 60     # seconds — non-driver conversation window

# === Feature flags ===
GROUP_TICKET_NOTIFICATIONS = True  # Ticket yuborilsa groupga ham xabar chiqadi
