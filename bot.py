import ssl
import certifi
import asyncio
import aiohttp
import aiosqlite
import json
from google import genai
from google.genai import types as genai_types
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from openai import OpenAI
import re
import requests
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from pydantic import BaseModel
import uvicorn
from contextlib import asynccontextmanager
import os
import fitz  # PyMuPDF
import httpx
from io import BytesIO
from PIL import Image
import threading
from typing import Optional
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === Token Cache File ===
TOKEN_CACHE_FILE = Path("groups_token_cache.json")

# === SQLite Database ===
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot_data.db"
STARTED_GROUPS_FILE = DATA_DIR / "started_groups.json"


def load_all_group_tokens():
    """All guruhlar tokenlarini fayldan o'qish"""
    if TOKEN_CACHE_FILE.exists():
        try:
            return json.loads(TOKEN_CACHE_FILE.read_text())
        except:
            return {}
    return {}


def save_all_group_tokens(data):
    """All guruhlar tokenlarini faylga saqlash"""
    TOKEN_CACHE_FILE.write_text(json.dumps(data, indent=2))


def load_started_groups() -> set:
    """/start bosilgan guruhlarni fayldan yuklash"""
    if STARTED_GROUPS_FILE.exists():
        try:
            return set(json.loads(STARTED_GROUPS_FILE.read_text()))
        except Exception:
            return set()
    return set()


def save_started_groups():
    """STARTED_GROUPS to'plamini faylga saqlash"""
    try:
        STARTED_GROUPS_FILE.write_text(json.dumps(sorted(STARTED_GROUPS)))
    except Exception as e:
        print(f"⚠️ Failed to persist started_groups: {e}")


def mark_group_started(chat_id):
    """Guruhni /start bosilgan deb belgilash va diskka saqlash"""
    STARTED_GROUPS.add(chat_id)
    save_started_groups()


def save_group_token(group_id, token, group_name):
    """Bitta guruh tokenini saqlash"""
    data = load_all_group_tokens()
    data[str(group_id)] = {
        "token": token,
        "group_name": group_name,
        "saved_at": datetime.now().isoformat()
    }
    save_all_group_tokens(data)
    print(f"💾 Token saved for group {group_id}")


def get_group_token(group_id):
    """Guruh tokenini fayldan olish"""
    data = load_all_group_tokens()
    group_data = data.get(str(group_id))
    if group_data:
        return group_data.get("token")
    return None


async def save_driver_id(group_id, driver_id, driver_name):
    """Driver ID ni DB ga saqlash"""
    group_id_str = str(group_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO groups (group_id, driver_id, driver_name, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(group_id) DO UPDATE SET
                driver_id   = excluded.driver_id,
                driver_name = excluded.driver_name,
                updated_at  = excluded.updated_at
        """, (group_id_str, driver_id, driver_name))
        await db.commit()
    GROUP_DRIVER_IDS[group_id_str] = driver_id
    print(f"💾 Driver {driver_name} (ID: {driver_id}) saved in DB for group {group_id}")


async def save_team_driver_id(group_id, driver_id, driver_name):
    """Team driver ID ni DB ga saqlash"""
    group_id_str = str(group_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE groups SET team_driver_id=?, team_driver_name=?, updated_at=datetime('now') WHERE group_id=?",
            (driver_id, driver_name, group_id_str)
        )
        await db.commit()
    GROUP_TEAM_DRIVERS[group_id_str] = driver_id
    print(f"💾 Team driver {driver_name} (ID: {driver_id}) saved in DB for group {group_id}")


async def remove_team_driver(group_id):
    """Team driverni DB va RAM dan o'chirish"""
    group_id_str = str(group_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE groups SET team_driver_id=NULL, team_driver_name=NULL, updated_at=datetime('now') WHERE group_id=?",
            (group_id_str,)
        )
        await db.commit()
    GROUP_TEAM_DRIVERS.pop(group_id_str, None)
    print(f"🗑️ Team driver removed for group {group_id}")


def get_group_driver(group_id) -> int | None:
    """RAM dan guruh asosiy driver ID ni olish"""
    return GROUP_DRIVER_IDS.get(str(group_id))


def get_team_driver(group_id) -> int | None:
    """RAM dan guruh team driver ID ni olish"""
    return GROUP_TEAM_DRIVERS.get(str(group_id))


def is_any_driver(group_id, user_id) -> bool:
    """User asosiy yoki team driver ekanligini tekshirish"""
    gid = str(group_id)
    return user_id == GROUP_DRIVER_IDS.get(gid) or user_id == GROUP_TEAM_DRIVERS.get(gid)


def remove_group_token(group_id):
    """Guruh tokenini o'chirish (muddati o'tganda)"""
    data = load_all_group_tokens()
    if str(group_id) in data:
        del data[str(group_id)]
        save_all_group_tokens(data)
        print(f"🗑️ Token removed for group {group_id}")


def save_group_ticket_status(group_id, status):
    """Ticket statusni groups_token_cache.json ga saqlash"""
    data = load_all_group_tokens()
    group_id_str = str(group_id)
    if group_id_str in data:
        data[group_id_str]["ticket_status"] = status
        data[group_id_str]["ticket_status_updated_at"] = datetime.now().isoformat()
        save_all_group_tokens(data)
        print(f"💾 Ticket status '{status}' saved for group {group_id}")


def get_group_ticket_status(group_id):
    """Guruh uchun ticket statusni fayldan olish"""
    data = load_all_group_tokens()
    group_data = data.get(str(group_id))
    if group_data:
        return group_data.get("ticket_status", "")
    return ""


# === SQLite DB init ===

async def init_db():
    """SQLite database yaratish va jadvallarni sozlash"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS loads (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id       TEXT NOT NULL,
                load_id        TEXT NOT NULL,
                pickup_count   INTEGER NOT NULL DEFAULT 1,
                delivery_count INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(group_id, load_id)
            );

            CREATE TABLE IF NOT EXISTS bols (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id   TEXT NOT NULL,
                load_id    TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                file_blob  BLOB NOT NULL,
                accepted   INTEGER NOT NULL DEFAULT 0,
                saved_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pods (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id   TEXT NOT NULL,
                load_id    TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                file_blob  BLOB NOT NULL,
                saved_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_bols_group_load ON bols(group_id, load_id);
            CREATE INDEX IF NOT EXISTS idx_pods_group_load ON pods(group_id, load_id);

            CREATE TABLE IF NOT EXISTS groups (
                group_id         TEXT PRIMARY KEY,
                driver_id        INTEGER,
                driver_name      TEXT,
                team_driver_id   INTEGER,
                team_driver_name TEXT,
                updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS company_permissions (
                company_id              TEXT PRIMARY KEY,
                ticket_create           INTEGER NOT NULL DEFAULT 1,
                task_paraphrase         INTEGER NOT NULL DEFAULT 0,
                bol_pod_paperwork       INTEGER NOT NULL DEFAULT 1,
                check_in_check_out      INTEGER NOT NULL DEFAULT 1,
                sleep_time              INTEGER NOT NULL DEFAULT 1,
                photo_pdf               INTEGER NOT NULL DEFAULT 1,
                created_at              TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
            );
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
        """)
        await db.commit()
        # Migration: mavjud DB ga photo_pdf columnini qo'shish
        try:
            await db.execute(
                "ALTER TABLE company_permissions ADD COLUMN photo_pdf INTEGER NOT NULL DEFAULT 1"
            )
            await db.commit()
        except Exception:
            pass  # Column allaqachon mavjud
        # Migration: groups jadvaliga team_driver ustunlarini qo'shish
        for col_def in [
            "ALTER TABLE groups ADD COLUMN team_driver_id INTEGER",
            "ALTER TABLE groups ADD COLUMN team_driver_name TEXT",
        ]:
            try:
                await db.execute(col_def)
                await db.commit()
            except Exception:
                pass  # Column allaqachon mavjud
    print("✅ SQLite DB initialized:", DB_PATH)


# === Async DB funksiyalari ===

async def init_load_in_cache(group_id, load_id, pickup_count, delivery_count):
    """Load ni DB da yaratish (agar mavjud bo'lmasa)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO loads (group_id, load_id, pickup_count, delivery_count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(group_id, load_id) DO NOTHING""",
            (str(group_id), str(load_id), pickup_count, delivery_count)
        )
        await db.commit()
    print(f"💾 Load {load_id} initialized for group {group_id} (pickups: {pickup_count}, deliveries: {delivery_count})")


async def add_bol_to_cache(group_id, load_id, message_id, file_bytes: bytes):
    """BOL ni DB ga qo'shish (bytes qabul qiladi)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bols (group_id, load_id, message_id, file_blob) VALUES (?, ?, ?, ?)",
            (str(group_id), str(load_id), message_id, file_bytes)
        )
        await db.commit()
    print(f"💾 BOL added for group {group_id}, load {load_id}")


async def add_pod_to_cache(group_id, load_id, message_id, file_bytes: bytes):
    """POD ni DB ga qo'shish (bytes qabul qiladi)"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO pods (group_id, load_id, message_id, file_blob) VALUES (?, ?, ?, ?)",
            (str(group_id), str(load_id), message_id, file_bytes)
        )
        await db.commit()
    print(f"💾 POD added for group {group_id}, load {load_id}")


async def get_bols_count(group_id, load_id) -> int:
    """Load uchun BOL lar sonini olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM bols WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_pods_count(group_id, load_id) -> int:
    """Load uchun POD lar sonini olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM pods WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_pickup_count(group_id, load_id) -> int:
    """Load uchun kerakli pickup (BOL) sonini olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pickup_count FROM loads WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1


async def get_delivery_count(group_id, load_id) -> int:
    """Load uchun kerakli delivery (POD) sonini olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT delivery_count FROM loads WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1


async def get_last_bol(group_id, load_id) -> bytes | None:
    """Oxirgi BOL ni bytes sifatida olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT file_blob FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            return bytes(row[0]) if row else None


async def all_bols_accepted(group_id, load_id) -> bool:
    """Barcha BOL lar accepted bo'lganmi tekshirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(accepted) FROM bols WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            count, accepted_sum = row if row else (0, 0)
            return count > 0 and (accepted_sum or 0) == count


async def is_bol_accepted(group_id, load_id) -> bool:
    """Oxirgi BOL accepted bo'lganmi tekshirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT accepted FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False


async def set_last_bol_accepted(group_id, load_id, accepted=True):
    """Oxirgi BOL ni accepted deb belgilash"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE bols SET accepted=? WHERE id=?",
                (1 if accepted else 0, row[0])
            )
            await db.commit()
            print(f"✅ Last BOL accepted={accepted} for group {group_id}, load {load_id}")
            return True
    return False


async def set_bol_accepted(group_id, load_id, accepted=True):
    """Oxirgi BOL ni accepted deb belgilash"""
    return await set_last_bol_accepted(group_id, load_id, accepted)


async def needs_more_bols(group_id, load_id) -> bool:
    """Yana BOL kerakmi tekshirish"""
    count = await get_bols_count(group_id, load_id)
    pickup = await get_pickup_count(group_id, load_id)
    return count < pickup


async def can_accept_pod(group_id, load_id) -> bool:
    """POD qabul qilish mumkinmi (barcha BOL lar accepted bo'lishi kerak)"""
    count = await get_bols_count(group_id, load_id)
    pickup = await get_pickup_count(group_id, load_id)
    if count < pickup:
        return False
    return await all_bols_accepted(group_id, load_id)


async def has_bol_for_load(group_id, load_id) -> bool:
    """Load uchun BOL bormi tekshirish"""
    return await get_bols_count(group_id, load_id) > 0


async def has_pods_for_load(group_id, load_id) -> bool:
    """Load uchun POD bormi tekshirish"""
    return await get_pods_count(group_id, load_id) > 0


async def remove_last_bol_for_load(group_id, load_id):
    """Load uchun oxirgi BOL ni o'chirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.execute("DELETE FROM bols WHERE id=?", (row[0],))
            await db.commit()
            print(f"🗑️ Last BOL removed for group {group_id}, load {load_id}")
            return True
    return False


async def remove_last_pod_for_load(group_id, load_id):
    """Load uchun oxirgi POD ni o'chirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM pods WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.execute("DELETE FROM pods WHERE id=?", (row[0],))
            await db.commit()
            print(f"🗑️ Last POD removed for group {group_id}, load {load_id}")
            return True
    return False


async def clear_load_from_cache(group_id, load_id):
    """Load ni DB dan o'chirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bols WHERE group_id=? AND load_id=?", (str(group_id), str(load_id)))
        await db.execute("DELETE FROM pods WHERE group_id=? AND load_id=?", (str(group_id), str(load_id)))
        await db.execute("DELETE FROM loads WHERE group_id=? AND load_id=?", (str(group_id), str(load_id)))
        await db.commit()
    print(f"🗑️ Load {load_id} cleared for group {group_id}")


async def clear_all_loads_for_group(group_id):
    """Guruh uchun barcha load larni o'chirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bols WHERE group_id=?", (str(group_id),))
        await db.execute("DELETE FROM pods WHERE group_id=?", (str(group_id),))
        await db.execute("DELETE FROM loads WHERE group_id=?", (str(group_id),))
        await db.commit()
    print(f"🗑️ All loads cleared for group {group_id}")


def save_group_company_id(group_id, company_id):
    """Group uchun companyId ni groups_token_cache.json ga saqlash"""
    data = load_all_group_tokens()
    group_data = data.get(str(group_id), {})
    group_data["companyId"] = str(company_id)
    data[str(group_id)] = group_data
    save_all_group_tokens(data)
    print(f"💾 CompanyId {company_id} saved for group {group_id}")


def get_group_company_id(group_id):
    """Group uchun companyId ni olish"""
    data = load_all_group_tokens()
    return data.get(str(group_id), {}).get("companyId")


async def get_or_fetch_company_id(group_id) -> str | None:
    """companyId ni olish; yo'q bo'lsa by-group API dan olib saqlaydi."""
    company_id = get_group_company_id(group_id)
    if company_id:
        return str(company_id)
    # API dan olishga urinish
    global ACCESS_TOKEN
    if not ACCESS_TOKEN:
        await get_api_token()
    url = f"{BASE_URL}/group-links/{group_id}/by-group"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    cid = data.get("companyId") or data.get("company_id")
                    if cid:
                        save_group_company_id(group_id, cid)
                        print(f"💾 companyId {cid} fetched and saved for group {group_id}")
                        return str(cid)
    except Exception as e:
        await send_error_to_group(f"❌ get_or_fetch_company_id error: {e}", group_id=group_id)
    return None


async def get_company_permissions(company_id: str) -> dict | None:
    """company_id bo'yicha permissions olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ticket_create, task_paraphrase, bol_pod_paperwork, check_in_check_out, sleep_time, photo_pdf, created_at, updated_at "
            "FROM company_permissions WHERE company_id=?",
            (str(company_id),)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "companyId": int(company_id),
                "ticketCreate": bool(row[0]),
                "taskParaphrase": bool(row[1]),
                "bolPodPaperworkAnalysis": bool(row[2]),
                "checkInCheckOut": bool(row[3]),
                "sleepTime": bool(row[4]),
                "photoPdf": bool(row[5]),
                "createdAt": row[6],
                "updatedAt": row[7],
            }


async def get_load_from_cache(group_id, load_id):
    """Load ma'lumotlarini DB dan olish (pickup_count, delivery_count)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pickup_count, delivery_count FROM loads WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id))
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"pickup_count": row[0], "delivery_count": row[1]}
    return None


# === Accepted status tracking ===
ACCEPTED_STATUS = {}  # group_id -> True/False

# === SSL & API Keys ===
ssl_context = ssl.create_default_context(cafile=certifi.where())

# ====== YOUR KEYS (from .env) ======
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# === Gemini setup ===
genai_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_BOT_MODEL = "gemini-2.5-flash"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# === Error Logger Bot ===
ERROR_BOT_TOKEN = os.environ.get("ERROR_BOT_TOKEN", "")
ERROR_GROUP_ID = int(os.environ.get("ERROR_GROUP_ID", "-5190421008"))
error_bot = Bot(token=ERROR_BOT_TOKEN)


async def send_error_to_group(message: str, group_id=None):
    if not ERROR_GROUP_ID:
        return
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        group_label = ""
        if group_id is not None:
            gid_str = str(group_id)
            data = load_all_group_tokens()
            group_name = data.get(gid_str, {}).get("group_name", "")
            if group_name:
                group_label = f" <b>[{group_name}]</b>"
            else:
                group_label = f" <b>[group:{gid_str}]</b>"
        await error_bot.send_message(ERROR_GROUP_ID, f"❌ <b>[bot]</b>{group_label} {message}\n🕐 {now}", parse_mode="HTML")
    except Exception:
        pass
client = OpenAI(api_key=OPENAI_API_KEY)
groq_client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

# === FastAPI Application ===
message_queue = asyncio.Queue()

# === Cooldown timeouts (30 seconds) ===
DRIVER_COOLDOWN = {}  # driver_id -> last_ticket_time
COOLDOWN_DURATION = 30  # seconds

# === Conversation tracking ===
CONVERSATION_LAST_TIME = {}  # group_id -> last_conversation_time
CONVERSATION_TIMEOUT = 60  # 1 minute - faqat non-driverlar uchun

# === Last tickets for similarity check ===
LAST_TICKETS = {}  # group_id -> last_ticket_text

# === Group ticket status tracking ===
GROUP_TICKET_STATUS = {}  # group_id -> {"status": "todo"/"done", "created_at": timestamp, "done_at": timestamp}
GROUP_TICKET_TIMERS = {}  # group_id -> timer_task for resetting status

# === Group ticket messages collection ===
GROUP_TICKET_MESSAGES = {}  # group_id -> [{"writerName": str, "message": str, "timestamp": str}, ...]
GROUP_TICKET_POLLING_TASKS = {}  # group_id -> polling_task for checking backend status

# === Group ticket notifications ===
GROUP_TICKET_NOTIFICATIONS = True  # Ticket yuborilsa groupga ham xabar chiqadi

# === Failed messages queue (retry mechanism) ===
FAILED_MESSAGES_QUEUE = []  # [{"groupId": str, "writerName": str, "message": str}, ...]
HISTORY_SENT_MESSAGE_KEYS = set()  # {"<chat_id>:<message_id>", ...}


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 FastAPI starting up...")
    await init_db()
    bot_task = asyncio.create_task(run_bot())
    print("🤖 Telegram bot started in background")
    yield
    print("🛑 FastAPI shutting down...")
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        print("🤖 Telegram bot stopped")


app = FastAPI(
    title="Telegram Group Message API",
    description="Bu API orqali **groupId** yuborib Telegram guruhiga xabar jo'natish mumkin.",
    version="1.0.0",
    lifespan=lifespan
)


class MessageRequest(BaseModel):
    group_id: str
    message: str
    has_pin_required: bool = False


class MessageResponse(BaseModel):
    success: bool
    message: str
    message_id: int = None
    pinned: bool = False


@app.post("/send-message", response_model=MessageResponse)
async def send_message(data: MessageRequest):
    await message_queue.put(data)
    return MessageResponse(
        success=True,
        message="Xabar navbatga qo'yildi",
        pinned=False
    )


class TicketStatusRequest(BaseModel):
    group_id: str
    status: str  # "done" or empty string


class TicketStatusResponse(BaseModel):
    success: bool
    message: str


class PermissionsRequest(BaseModel):
    companyId: int
    ticketCreate: bool = True
    taskParaphrase: bool = False
    bolPodPaperworkAnalysis: bool = True
    checkInCheckOut: bool = True
    sleepTime: bool = True
    photoPdf: bool = True


class PermissionsUpdateRequest(BaseModel):
    ticketCreate: bool = True
    taskParaphrase: bool = False
    bolPodPaperworkAnalysis: bool = True
    checkInCheckOut: bool = True
    sleepTime: bool = True
    photoPdf: bool = True


class PermissionsResponse(BaseModel):
    companyId: int
    ticketCreate: bool
    taskParaphrase: bool
    bolPodPaperworkAnalysis: bool
    checkInCheckOut: bool
    sleepTime: bool
    photoPdf: bool
    createdAt: str
    updatedAt: str


class AcceptedRequest(BaseModel):
    group_id: str
    load_id: str  # Load ID - qaysi load uchun
    status: str  # "accepted", "rejected" yoki "completed"
    message: str = ""  # Backend dan kelgan xabar


class AcceptedResponse(BaseModel):
    success: bool
    message: str
    status: str  # "accepted", "rejected" or "completed"


def is_group_ticket_todo(group_id: str | int) -> bool:
    """Group ticket status TODO ekanini tekshiradi."""
    ticket_data = GROUP_TICKET_STATUS.get(str(group_id), {})
    return ticket_data.get("status") == "todo"


def _history_message_key(msg: types.Message) -> str:
    """History API uchun dedup key."""
    return f"{msg.chat.id}:{msg.message_id}"


def _build_history_message_text(msg: types.Message, fallback_text: str | None = None) -> str:
    """Har xil content turini history API uchun bitta matnga aylantirish."""
    if fallback_text:
        return fallback_text

    if msg.text:
        return msg.text.strip()

    if msg.caption:
        return msg.caption.strip()

    content_type = str(getattr(msg, "content_type", "unknown")).lower()
    content_map = {
        "photo": "[PHOTO]",
        "document": f"[DOCUMENT] {(msg.document.file_name if msg.document else '')}".strip(),
        "voice": "[VOICE MESSAGE]",
        "audio": f"[AUDIO] {(msg.audio.file_name if msg.audio else '')}".strip(),
        "video": "[VIDEO]",
        "video_note": "[VIDEO NOTE]",
        "animation": "[ANIMATION/GIF]",
        "sticker": "[STICKER]",
        "location": "[LOCATION]",
        "contact": "[CONTACT]",
        "poll": "[POLL]",
    }
    return content_map.get(content_type, f"[{content_type.upper()}]")


async def forward_message_to_history_if_todo(msg: types.Message, fallback_text: str | None = None) -> bool:
    """
    Ticket status TODO bo'lsa message'ni history API'ga yuboradi.
    True qaytsa, message TODO oqimida qayta ishlangan hisoblanadi.
    """
    if not is_group_ticket_todo(msg.chat.id):
        print(f"⏭️ [HISTORY] Group {msg.chat.id} ticket not TODO, skipping history")
        return False

    message_key = _history_message_key(msg)
    if message_key in HISTORY_SENT_MESSAGE_KEYS:
        print(f"⏭️ [HISTORY] Duplicate message {message_key}, skipping")
        return True

    history_text = _build_history_message_text(msg, fallback_text=fallback_text)
    print(f"📤 [HISTORY] Sending to history API | group={msg.chat.id} | user={msg.from_user.full_name} | text={history_text[:60]!r}")
    success = await send_message_to_history_api(
        group_id=msg.chat.id,
        writer_name=msg.from_user.full_name,
        message=history_text
    )
    if not success:
        await send_error_to_group(f"❌ [HISTORY] Failed to send message {message_key} to history API")

    HISTORY_SENT_MESSAGE_KEYS.add(message_key)
    if len(HISTORY_SENT_MESSAGE_KEYS) > 10000:
        HISTORY_SENT_MESSAGE_KEYS.clear()
    return True




async def reset_group_ticket_status(group_id: str, delay_minutes: int = 4):
    """4 minut keyin group status'ini reset qilish"""
    group_id_str = str(group_id)
    try:
        # Avvalgi timer ni cancel qilish
        if group_id_str in GROUP_TICKET_TIMERS:
            GROUP_TICKET_TIMERS[group_id_str].cancel()

        # Yangi timer yaratish
        async def reset_after_delay():
            await asyncio.sleep(delay_minutes * 60)
            if group_id_str in GROUP_TICKET_STATUS:
                GROUP_TICKET_STATUS[group_id_str]["status"] = ""  # Status ni empty qilish
                print(f"✅ Group {group_id} ticket status reset to empty after {delay_minutes} minutes")

        task = asyncio.create_task(reset_after_delay())
        GROUP_TICKET_TIMERS[group_id_str] = task
    except Exception as e:
        await send_error_to_group(f"❌ Error setting reset timer for group {group_id}: {e}", group_id=group_id)


async def poll_backend_ticket_status(group_id: str, ticket_id: str = None):
    """Backend'da ticket status'ni polling qilish, 'done' bo'lguncha"""
    group_id_str = str(group_id)
    poll_interval = 5  # 5 soniya oraligi bilan check qilish
    max_polls = 1440  # 2 soat (120 minut * 2)
    poll_count = 0

    try:
        print(f"🔄 Starting polling for group {group_id} ticket status...")

        while poll_count < max_polls:
            await asyncio.sleep(poll_interval)
            poll_count += 1

            # Backend API dan status olish - /ticket-status/{groupId} yoki /tickets ga sorov
            # Hozirda GROUP_TICKET_STATUS memory'da saqlangan, real backend API olish uchun:
            # actual_status = await check_backend_ticket_status(group_id)

            if group_id_str in GROUP_TICKET_STATUS:
                current_status = GROUP_TICKET_STATUS[group_id_str].get("status", "")

                # Status "done" bo'lganda - habarlarni backend'ga jo'natish
                if current_status == "done":
                    print(f"✅ Group {group_id} ticket status changed to 'done'. Sending collected messages...")
                    await send_collected_messages_to_api(group_id)

                    # Status'ni groups_token_cache.json ga saqlash
                    save_group_ticket_status(group_id, "done")

                    # Polling taskni bekor qilish
                    if group_id_str in GROUP_TICKET_POLLING_TASKS:
                        del GROUP_TICKET_POLLING_TASKS[group_id_str]

                    break

                # Status hali "todo" - davom etish
                if poll_count % 20 == 0:  # Har 100 soniyada log
                    msg_count = len(GROUP_TICKET_MESSAGES.get(group_id_str, []))
                    print(f"⏳ Polling group {group_id}: status='todo', collected {msg_count} messages")

    except asyncio.CancelledError:
        print(f"⚠️ Polling cancelled for group {group_id}")
    except Exception as e:
        await send_error_to_group(f"❌ Error during polling for group {group_id}: {e}", group_id=group_id)
    finally:
        # Polling taskni cleanup qilish
        if group_id_str in GROUP_TICKET_POLLING_TASKS:
            del GROUP_TICKET_POLLING_TASKS[group_id_str]


async def send_message_to_history_api(group_id: str, writer_name: str, message: str):
    """Habarni darhol historyticketapi'ga jo'natish. Muvaffaqiyatsiz bo'lsa FAILED_MESSAGES_QUEUE'ga qo'shish."""
    payload = {
        "groupId": str(group_id),
        "writerName": writer_name,
        "message": message
    }

    token = await get_api_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "EN",
        "Content-Type": "application/json",
        "X-Group-Id": str(group_id)
    }
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
                async with session.post(
                        "https://api.abstract-it.uz/api/tickets/history",
                        json=payload,
                        headers=headers,
                        timeout=10
                ) as resp:
                    if resp.status in (200, 201):
                        print(f"✅ [HISTORY API] Sent | group={group_id} | writer={writer_name}")
                        return True
                    elif resp.status == 404:
                        print(f"⚠️ [HISTORY API] Not found [404] | group={group_id} | writer={writer_name} | Dropping message")
                        return False
                    else:
                        resp_text = await resp.text()
                        print(f"⚠️ [HISTORY API] Error [{resp.status}] | group={group_id} | attempt={attempt + 1}/3 | response={resp_text[:200]}")
        except asyncio.TimeoutError:
            print(f"⚠️ [HISTORY API] Timeout | group={group_id} | attempt={attempt + 1}/3")
        except Exception as e:
            print(f"⚠️ [HISTORY API] Exception | group={group_id} | attempt={attempt + 1}/3 | error={e}")

        if attempt < 2:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s

    # 3 urinishdan keyin ham bo'lmasa — queue'ga qo'shish
    await send_error_to_group(f"❌ [HISTORY API] All 3 attempts failed | writer={writer_name} | Adding to retry queue", group_id=group_id)
    FAILED_MESSAGES_QUEUE.append(payload)
    return False


async def retry_failed_messages():
    """Har 30 soniyada muvaffaqiyatsiz xabarlarni qayta yuborish"""
    while True:
        await asyncio.sleep(30)
        if not FAILED_MESSAGES_QUEUE:
            continue

        retry_list = FAILED_MESSAGES_QUEUE.copy()
        FAILED_MESSAGES_QUEUE.clear()

        print(f"🔁 Retrying {len(retry_list)} failed message(s)...")
        for payload in retry_list:
            try:
                token = await get_api_token()
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept-Language": "EN",
                    "Content-Type": "application/json",
                    "X-Group-Id": str(payload['groupId'])
                }
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
                    async with session.post(
                            "https://api.abstract-it.uz/api/tickets/history",
                            json=payload,
                            headers=headers,
                            timeout=10
                    ) as resp:
                        if resp.status in (200, 201):
                            print(f"✅ Retry successful for group {payload['groupId']}")
                        elif resp.status == 404:
                            print(f"⚠️ Retry failed [404] for group {payload['groupId']}. Dropping message.")
                        else:
                            print(f"⚠️ Retry failed [{resp.status}] for group {payload['groupId']}. Re-queuing.")
                            FAILED_MESSAGES_QUEUE.append(payload)
            except Exception as e:
                print(f"⚠️ Retry error for group {payload['groupId']}: {e}. Re-queuing.")
                FAILED_MESSAGES_QUEUE.append(payload)


async def send_collected_messages_to_api(group_id: str):
    """Placeholder - habarlar darhol API'ga yuboriladi"""
    print(f"ℹ️ All messages sent directly to API for group {group_id}")


async def ticket_status_api(data: TicketStatusRequest):
    """
    Ticket status'ini update qilish API'si.
    Backend menga POST qiladi:

    POST /update-ticket-status
    {
        "group_id": "string",
        "status": "done" yoki empty string ""
    }
    """
    try:
        group_id_str = str(data.group_id)
        status = data.status.strip().lower()

        if status == "done":
            # Status ni "done" qilish
            GROUP_TICKET_STATUS[group_id_str] = {
                "status": "done",
                "created_at": GROUP_TICKET_STATUS.get(group_id_str, {}).get("created_at", datetime.now().isoformat()),
                "done_at": datetime.now().isoformat()
            }
            print(f"✅ Group {group_id_str} ticket status set to 'done'")

            # DARHOL habarlarni API'ga jo'natish
            await send_collected_messages_to_api(group_id_str)

            # Status'ni groups_token_cache.json ga saqlash
            save_group_ticket_status(group_id_str, "done")

            # Polling taskni cancel qilish (agar mavjud bo'lsa)
            if group_id_str in GROUP_TICKET_POLLING_TASKS:
                polling_task = GROUP_TICKET_POLLING_TASKS[group_id_str]
                polling_task.cancel()
                del GROUP_TICKET_POLLING_TASKS[group_id_str]
                print(f"⛔ Polling task cancelled for group {group_id_str}")

            # 1 sekund keyin status ni reset qilish
            asyncio.create_task(reset_group_ticket_status(group_id_str, delay_minutes=0.0167))

            return TicketStatusResponse(
                success=True,
                message=f"Group {group_id_str} status set to 'done'. Messages sent to API. Will reset in 1 second."
            )
        elif status == "" or status == "empty":
            # Status ni empty qilish
            if group_id_str in GROUP_TICKET_STATUS:
                GROUP_TICKET_STATUS[group_id_str]["status"] = ""
            print(f"✅ Group {group_id_str} ticket status set to empty")

            return TicketStatusResponse(
                success=True,
                message=f"Group {group_id_str} status set to empty"
            )
        else:
            return TicketStatusResponse(
                success=False,
                message=f"Invalid status: {data.status}. Use 'done' or empty string"
            )
    except Exception as e:
        await send_error_to_group(f"❌ Error in ticket_status_api: {e}")
        return TicketStatusResponse(
            success=False,
            message=f"Error: {str(e)}"
        )


@app.post("/update-ticket-status", response_model=TicketStatusResponse)
async def update_ticket_status(data: TicketStatusRequest):
    """
    Backend'dan POST chaqiruvi - Ticket status'ini update qilish

    POST /update-ticket-status
    {
        "group_id": "-1003883719322",
        "status": "done"
    }
    """
    return await ticket_status_api(data)


@app.get("/ticket-status/{group_id}")
async def get_ticket_status(group_id: str):
    """
    Ticket status'ini tekshirish - GET endpoint

    GET /ticket-status/-1003883719322
    """
    try:
        group_id_str = str(group_id)

        if group_id_str in GROUP_TICKET_STATUS:
            ticket_data = GROUP_TICKET_STATUS[group_id_str]
            messages_count = len(GROUP_TICKET_MESSAGES.get(group_id_str, []))

            return {
                "success": True,
                "group_id": group_id_str,
                "status": ticket_data.get("status", ""),
                "created_at": ticket_data.get("created_at"),
                "done_at": ticket_data.get("done_at"),
                "collected_messages": messages_count
            }
        else:
            return {
                "success": True,
                "group_id": group_id_str,
                "status": "",
                "message": "No active ticket for this group"
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


@app.get("/driver/{group_id}")
async def get_driver_by_group(group_id: str):
    """
    Guruh bo'yicha driver ma'lumotlarini olish

    GET /driver/-1003883719322
    """
    group_id_str = str(group_id)
    driver_id = get_group_driver(group_id_str)
    driver_name = None

    data = load_all_group_tokens()
    group_data = data.get(group_id_str, {})
    driver_name = group_data.get("driver_name")
    company_id = group_data.get("companyId")

    if driver_id:
        return {
            "success": True,
            "group_id": group_id_str,
            "driver_id": driver_id,
            "driver_name": driver_name,
            "company_id": company_id
        }
    else:
        return {
            "success": False,
            "group_id": group_id_str,
            "message": "No driver set for this group"
        }


@app.post("/permissions", response_model=PermissionsResponse)
async def create_permissions(data: PermissionsRequest):
    """Company uchun permissions yaratish"""
    now = datetime.now().isoformat()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO company_permissions
                   (company_id, ticket_create, task_paraphrase, bol_pod_paperwork, check_in_check_out, sleep_time, photo_pdf, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(data.companyId), int(data.ticketCreate), int(data.taskParaphrase),
                 int(data.bolPodPaperworkAnalysis), int(data.checkInCheckOut), int(data.sleepTime),
                 int(data.photoPdf), now, now)
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail=f"Permissions already exist for company {data.companyId}")
    print(f"✅ Permissions created for company {data.companyId}")
    return PermissionsResponse(
        companyId=data.companyId,
        ticketCreate=data.ticketCreate,
        taskParaphrase=data.taskParaphrase,
        bolPodPaperworkAnalysis=data.bolPodPaperworkAnalysis,
        checkInCheckOut=data.checkInCheckOut,
        sleepTime=data.sleepTime,
        photoPdf=data.photoPdf,
        createdAt=now,
        updatedAt=now,
    )


@app.put("/permissions/{company_id}", response_model=PermissionsResponse)
async def update_permissions(company_id: int, data: PermissionsUpdateRequest):
    """Company permissions ni yangilash"""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO company_permissions
               (company_id, ticket_create, task_paraphrase, bol_pod_paperwork, check_in_check_out, sleep_time, photo_pdf, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(company_id) DO UPDATE SET
                 ticket_create=excluded.ticket_create,
                 task_paraphrase=excluded.task_paraphrase,
                 bol_pod_paperwork=excluded.bol_pod_paperwork,
                 check_in_check_out=excluded.check_in_check_out,
                 sleep_time=excluded.sleep_time,
                 photo_pdf=excluded.photo_pdf,
                 updated_at=excluded.updated_at""",
            (str(company_id), int(data.ticketCreate), int(data.taskParaphrase),
             int(data.bolPodPaperworkAnalysis), int(data.checkInCheckOut), int(data.sleepTime),
             int(data.photoPdf), now, now)
        )
        await db.commit()
    print(f"✅ Permissions updated for company {company_id}")
    perms = await get_company_permissions(str(company_id))
    return PermissionsResponse(**perms)


@app.get("/permissions/{company_id}", response_model=PermissionsResponse)
async def get_permissions(company_id: int):
    """Company permissions ni olish"""
    perms = await get_company_permissions(str(company_id))
    if not perms:
        raise HTTPException(status_code=404, detail=f"Permissions not found for company {company_id}")
    return PermissionsResponse(**perms)


@app.delete("/group-deleted/{group_id}")
async def group_deleted_webhook(group_id: str):
    """Backend guruhni o'chirganda chaqiradi — local ma'lumotlarni tozalash"""
    group_id_str = str(group_id)
    print(f"🗑️ Webhook: group {group_id_str} deleted from backend, cleaning up...")

    # JSON cache dan o'chirish
    remove_group_token(group_id_str)

    # RAM cache lardan o'chirish
    REGISTERED_GROUPS.pop(group_id_str, None)
    try:
        REGISTERED_GROUPS.pop(int(group_id_str), None)
    except Exception:
        pass
    AWAITING_TOKEN.pop(group_id_str, None)
    GROUP_DRIVER_IDS.pop(group_id_str, None)

    # SQLite DB dan o'chirish
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM groups WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM loads WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM bols WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM pods WHERE group_id=?", (group_id_str,))
        await db.commit()

    print(f"✅ Cleanup done for group {group_id_str}")
    return {"success": True, "groupId": group_id_str}


@app.get("/company/{company_id}/groups")
async def get_company_groups(company_id: int):
    """CompanyId bo'yicha bot da ro'yxatdan o'tgan grouplar"""
    data = load_all_group_tokens()
    groups = [
        {
            "groupId": group_id,
            "groupName": info.get("group_name"),
            "savedAt": info.get("saved_at"),
        }
        for group_id, info in data.items()
        if str(info.get("companyId")) == str(company_id)
    ]
    return {
        "companyId": company_id,
        "count": len(groups),
        "groups": groups,
    }


@app.get("/load-status/{group_id}/{load_id}")
async def get_load_status(group_id: str, load_id: str):
    """
    Load ning hozirgi holati (DB cache dan)

    GET /load-status/-1003883719322/12345
    """
    bols_count = await get_bols_count(group_id, load_id)
    pods_count = await get_pods_count(group_id, load_id)
    pickup_count = await get_pickup_count(group_id, load_id)
    delivery_count = await get_delivery_count(group_id, load_id)
    all_accepted = await all_bols_accepted(group_id, load_id) if bols_count > 0 else False

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT accepted, saved_at FROM bols WHERE group_id=? AND load_id=? ORDER BY id",
            (str(group_id), str(load_id))
        ) as cursor:
            bols = [{"accepted": bool(row[0]), "saved_at": row[1]} async for row in cursor]

        async with db.execute(
            "SELECT saved_at FROM pods WHERE group_id=? AND load_id=? ORDER BY id",
            (str(group_id), str(load_id))
        ) as cursor:
            pods = [{"saved_at": row[0]} async for row in cursor]

    return {
        "group_id": group_id,
        "load_id": load_id,
        "pickup_count": pickup_count,
        "delivery_count": delivery_count,
        "bols": {"count": bols_count, "required": pickup_count, "all_accepted": all_accepted, "items": bols},
        "pods": {"count": pods_count, "required": delivery_count, "items": pods},
        "in_cache": bols_count > 0 or pods_count > 0,
    }


@app.post("/accepted", response_model=AcceptedResponse)
async def set_accepted_status(data: AcceptedRequest):
    """
    Load uchun accepted/rejected/completed statusni o'rnatish (Multi-BOL/POD support).

    - accepted: BOL yoki POD tasdiqlandi
                - BOL: oxirgi BOL accepted bo'ladi, agar hamma BOL to'plangan va accepted -> POD kutiladi
                - POD: oxirgi POD accepted, yana POD yoki completed kutiladi
    - rejected: Agar POD bor -> oxirgi POD o'chadi
                Agar faqat BOL bor -> oxirgi BOL o'chadi
    - completed: Shu load cache dan tozalanadi (faqat barcha BOL accepted va POD bor bo'lganda)
    """
    group_id = data.group_id
    load_id = data.load_id
    status = data.status.lower()
    custom_message = data.message.strip() if data.message else ""

    if status == "accepted":
        # Accepted - hujjat tasdiqlandi
        ACCEPTED_STATUS[f"{group_id}_{load_id}"] = True

        if await has_pods_for_load(group_id, load_id):
            # POD tasdiqlandi
            current_pods = await get_pods_count(group_id, load_id)
            required_pods = await get_delivery_count(group_id, load_id)
            print(f"✅ Group {group_id} Load {load_id} POD #{current_pods}/{required_pods} ACCEPTED")

            if custom_message:
                try:
                    await bot.send_message(chat_id=int(group_id), text=custom_message)
                except Exception as e:
                    print(f"⚠️ Could not send message to group: {e}")

            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} POD #{current_pods}/{required_pods} accepted",
                status="accepted"
            )
        elif await has_bol_for_load(group_id, load_id):
            # BOL tasdiqlandi - oxirgi BOL ni accepted qilish
            await set_bol_accepted(group_id, load_id, True)
            current_bols = await get_bols_count(group_id, load_id)
            required_bols = await get_pickup_count(group_id, load_id)
            all_accepted = await all_bols_accepted(group_id, load_id)

            print(
                f"✅ Group {group_id} Load {load_id} BOL #{current_bols}/{required_bols} ACCEPTED (all_accepted={all_accepted})")

            if custom_message:
                try:
                    await bot.send_message(chat_id=int(group_id), text=custom_message)
                except Exception as e:
                    print(f"⚠️ Could not send message to group: {e}")

            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} BOL #{current_bols}/{required_bols} accepted",
                status="accepted"
            )
        else:
            # Hech narsa yo'q
            return AcceptedResponse(
                success=False,
                message=f"No BOL or POD in cache for load {load_id}",
                status="error"
            )

    elif status == "rejected":
        # Rejected
        ACCEPTED_STATUS[f"{group_id}_{load_id}"] = False

        if await has_pods_for_load(group_id, load_id):
            # POD bor - faqat oxirgi POD ni o'chirish
            current_pods = await get_pods_count(group_id, load_id)
            required_pods = await get_delivery_count(group_id, load_id)
            await remove_last_pod_for_load(group_id, load_id)
            remaining_pods = await get_pods_count(group_id, load_id)
            print(
                f"❌ Group {group_id} Load {load_id} POD #{current_pods} REJECTED - remaining {remaining_pods}/{required_pods}")

            if custom_message:
                try:
                    await bot.send_message(chat_id=int(group_id), text=custom_message)
                except Exception as e:
                    print(f"⚠️ Could not send message to group: {e}")

            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} POD #{current_pods} rejected, waiting for new POD",
                status="rejected"
            )
        elif await has_bol_for_load(group_id, load_id):
            # Faqat BOL bor - oxirgi BOL ni o'chirish (hammani emas!)
            current_bols = await get_bols_count(group_id, load_id)
            required_bols = await get_pickup_count(group_id, load_id)
            await remove_last_bol_for_load(group_id, load_id)
            remaining_bols = await get_bols_count(group_id, load_id)
            print(
                f"❌ Group {group_id} Load {load_id} BOL #{current_bols} REJECTED - remaining {remaining_bols}/{required_bols}")

            if custom_message:
                try:
                    await bot.send_message(chat_id=int(group_id), text=custom_message)
                except Exception as e:
                    print(f"⚠️ Could not send message to group: {e}")

            return AcceptedResponse(
                success=True,
                message=f"Load {load_id} BOL #{current_bols} rejected, waiting for new BOL",
                status="rejected"
            )
        else:
            return AcceptedResponse(
                success=False,
                message=f"No BOL or POD in cache for load {load_id}",
                status="error"
            )

    elif status == "completed":
        # Completed - shu load cache dan tozalanadi (BOL/POD bo'lmasa ham)
        await clear_load_from_cache(group_id, load_id)
        ACCEPTED_STATUS[f"{group_id}_{load_id}"] = True
        print(f"✅ Group {group_id} Load {load_id} COMPLETED - load cleared from cache")

        if custom_message:
            try:
                await bot.send_message(chat_id=int(group_id), text=custom_message)
            except Exception as e:
                print(f"⚠️ Could not send completed message to group: {e}")

        return AcceptedResponse(
            success=True,
            message=f"Load {load_id} completed, cleared from cache",
            status="completed"
        )
    else:
        # Noto'g'ri status
        return AcceptedResponse(
            success=False,
            message=f"Invalid status: {data.status}. Use 'accepted', 'rejected' or 'completed'",
            status="error"
        )


# === Verify Delivery API ===
VERIFY_DELIVERY_URL = os.environ.get("VERIFY_DELIVERY_URL", "https://askai.test.abstract-it.uz/verify-delivery")


def _detect_file_info(data: bytes, base_name: str) -> tuple[str, str]:
    """Bytes magic number asosida fayl nomi va content-type aniqlash."""
    if data[:4] == b'%PDF':
        return f"{base_name}.pdf", "application/pdf"
    elif data[:2] == b'\xff\xd8':
        return f"{base_name}.jpg", "image/jpeg"
    elif data[:8] == b'\x89PNG\r\n\x1a\n':
        return f"{base_name}.png", "image/png"
    else:
        return f"{base_name}.pdf", "application/octet-stream"


async def verify_delivery(bol_bytes: bytes, pod_bytes: bytes) -> dict:
    """BOL va POD ni /verify-delivery API ga yuborish"""
    try:
        bol_name, bol_mime = _detect_file_info(bol_bytes, "bol")
        pod_name, pod_mime = _detect_file_info(pod_bytes, "pod")
        print(f"📤 verify_delivery: BOL={bol_name}({bol_mime}), POD={pod_name}({pod_mime})")
        files = {
            'bol': (bol_name, bol_bytes, bol_mime),
            'pod': (pod_name, pod_bytes, pod_mime),
        }

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(VERIFY_DELIVERY_URL, files=files)

            if response.status_code == 200:
                result = response.json()
                print(f"✅ Verify delivery success: {result}")
                return {"success": True, "data": result}
            else:
                await send_error_to_group(f"❌ Verify delivery error [{response.status_code}]: {response.text}")
                return {"success": False, "error": response.text}

    except httpx.TimeoutException as e:
        print(f"❌ Verify delivery TIMEOUT after 180s: {e}")
        await send_error_to_group(f"❌ Verify delivery TIMEOUT: {e}")
        return {"success": False, "error": f"Timeout: {str(e)}"}
    except Exception as e:
        await send_error_to_group(f"❌ Verify delivery exception: {e}")
        return {"success": False, "error": str(e)}


def _linkify_maps(text: str) -> str:
    """Google Maps URL ni manzil matnga berkitadi yoki 'Click here to view on map' qiladi.
    - 'Location: <manzil>\n📍 URL' → 'Location: <a href="URL">manzil</a>'
    - Boshqa URL lar → '<a href="URL">Click here to view on map</a>'"""
    import re
    def replace_with_address(m):
        prefix = m.group(1)
        address = m.group(2)
        url = m.group(3)
        return f'{prefix}<a href="{url}">{address}</a>'
    # 1. Location: prefix bilan kelgan URL lar
    text = re.sub(
        r'((?:Current location|Location):\s*)([^\n]+)\n📍\s*(https://www\.google\.com/maps\?q=[\d.,-]+)',
        replace_with_address,
        text
    )
    # 2. Qolgan bare URL lar
    text = re.sub(
        r'📍\s*(https://www\.google\.com/maps\?q=[\d.,-]+)',
        lambda m: f'<a href="{m.group(1)}">Click here to view on map</a>',
        text
    )
    text = re.sub(
        r'(?<!["\'])https://www\.google\.com/maps\?q=[\d.,-]+',
        lambda m: f'<a href="{m.group(0)}">Click here to view on map</a>',
        text
    )
    return text


async def message_worker():
    TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
    while True:
        try:
            data = await message_queue.get()
            message_text = _linkify_maps(data.message)
            send_payload = {"chat_id": data.group_id, "text": message_text, "parse_mode": "HTML"}
            try:
                send_response = requests.post(f"{TELEGRAM_API_BASE}/sendMessage", json=send_payload)
                send_result = send_response.json()
                if send_result.get("ok"):
                    message_id = send_result["result"]["message_id"]
                    print(f"📨 Message sent to group {data.group_id}: {data.message[:50]}...")
                    if data.has_pin_required:
                        pin_payload = {"chat_id": data.group_id, "message_id": message_id,
                                       "disable_notification": False}
                        pin_response = requests.post(f"{TELEGRAM_API_BASE}/pinChatMessage", json=pin_payload)
                        pin_result = pin_response.json()
                        if pin_result.get("ok"):
                            print(f"📌 Message pinned in group {data.group_id}")
                else:
                    await send_error_to_group(f"❌ Error sending message: {send_result.get('description', 'Unknown error')}", group_id=data.group_id)
            except Exception as e:
                await send_error_to_group(f"❌ Error sending message: {e}", group_id=data.group_id)
            message_queue.task_done()
        except Exception as e:
            await send_error_to_group(f"❌ Worker error: {e}")
            await asyncio.sleep(1)


# === Telegram Bot Global Variables ===
GROUP_DRIVER_IDS = {}   # group_id (str) -> primary driver_id (int)
GROUP_TEAM_DRIVERS = {}  # group_id (str) -> team driver_id (int)
AWAITING_TOKEN = {}
TOKEN_FAILED_ATTEMPTS = {}  # group_id -> token validatsiya xatolari soni (/start qayta bosilgunicha)
REGISTERED_GROUPS = {}
STARTED_GROUPS = set()  # /start muvaffaqiyatli ishlagan guruhlar
GROUP_PENDING_IMAGES = {}  # group_id (str) -> {"pages": [bytes, ...], "prompt_msg_id": int | None, "last_image_time": float}
GROUP_IMAGE_DEBOUNCE_TASKS = {}  # group_id (str) -> asyncio.Task
GROUP_IMAGE_TIMEOUT_TASKS = {}  # group_id (str) -> asyncio.Task

# === Departments & Swagger ===
DEPARTMENT_MAP = {"fleet": 0, "updater": 1, "dispatcher": 2, "insurance": 3, "safety": 4, "hr": 5, "accounting": 6}
PRIORITY_MAP = {"high": 0, "medium": 1, "low": 2}
STATUS_TODO = 0

BASE_URL = os.environ.get("BASE_URL", "https://api.abstract-it.uz/api")
LOGIN_URL = f"{BASE_URL}/auth/login"
SWAGGER_URL = f"{BASE_URL}/tickets"
VALIDATE_TOKEN_URL = f"{BASE_URL}/general-settings/validate-bot-token"
ACTION_LOGS_URL = f"{BASE_URL}/action-logs"
CHECKIN_CHECKOUT_URL = f"{BASE_URL}/stops/checkin-checkout"

# Checkin/Checkout pattern — BOL/POD + load, keyin check in/out vaqtlari
# Vaqt formati: "10:00 AM PST", "10:00AM", "10.00 AM", "1455", "1000 AM"
# Trailing 2-4 harfli TZ (masalan "DPT" typo ham) ixtiyoriy
_TIME_CAPTURE = r'(?:\d{1,2}[:.\-]\d{2}|\d{4})\s*(?:[AaPp]\.?\s*[Mm]\.?)?(?:\s+[A-Za-z]{2,4}\b)?'
# \s+ — har qanday whitespace (bo'sh joy yoki newline), same-line va multi-line variantlar
CHECKIN_CHECKOUT_PATTERN = re.compile(
    r'(BOL|POD)\s*#?\s*([\w\-]+)\s+'
    r'.*?check(?:ed|ing)?\s*[\-_]?\s*in[:\s]*(' + _TIME_CAPTURE + r')\s+'
    r'.*?check(?:ed|ing)?\s*[\-_]?\s*out[:\s]*(' + _TIME_CAPTURE + r')',
    re.IGNORECASE | re.DOTALL
)

TIMEZONE_MAP = {"est": 0, "edt": 0, "cst": 1, "cdt": 1, "mst": 2, "mdt": 2, "pst": 3, "pdt": 3}
TIMEZONE_UTC_OFFSET = {"est": -5, "edt": -4, "cst": -6, "cdt": -5, "mst": -7, "mdt": -6, "pst": -8, "pdt": -7}

import base64
LOGIN_CREDENTIALS = {
    "email": os.environ.get("LOGIN_EMAIL", "clarvio@abstract-it.uz"),
    "password": os.environ.get("LOGIN_PASSWORD", "0987654321")
}
ACCESS_TOKEN = None
TOKEN_EXPIRES_AT = 0  # Unix timestamp


def _decode_token_exp(token: str) -> float:
    """JWT tokendan exp (eskirish vaqti) ni olish, imzosiz"""
    try:
        payload = token.split('.')[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = json.loads(base64.b64decode(payload))
        return float(decoded.get('exp', 0))
    except Exception:
        return 0


SLEEP_TIMER_URL = f"{BASE_URL}/v1/drivers/sleep-timer/by-group"

# === Default Quick Buttons ===
DEFAULT_QUICK_BUTTONS = [
    "🚛 Vehicle Issue",
    "📦 Delivery Problem",
    "🔄 System Issue",
    "⚠️ Safety Concern",
    "💰 Payment Issue",
    "📋 HR Question"
]


# ======================================================
# === Checkin/Checkout Parser ===
# ======================================================
def parse_time_to_iso(time_str: str) -> str:
    """Vaqt stringini lokal timezone dan UTC ga convert qilib ISO 8601 ga o'girish.
    Qo'llab-quvvatlaydigan formatlar:
      - "10:00 AM PST" / "10:00 AM" / "10:00AM" / "10:00am pst"
      - "10.00 AM PST" / "10-00 AM" (nuqta/tire separator)
      - "10:00 a.m. PST" / "10:00 p.m." (nuqtali AM/PM)
      - "1455" / "1455 PST" (sof military)
      - "1000 AM" (4 raqam + AM/PM)
      - "14:30" (24-hour, AM/PM yo'q)
      - "04:50PM DPT" (noma'lum timezone suffix — strip qilinadi, default PST)
    TZ topilmasa default PST (-8). Parse bo'lmasa 00:00 fallback + warning."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fallback = f"{today}T00:00:00.000Z"
    if not time_str or not str(time_str).strip():
        return fallback

    original = str(time_str)
    s = original.strip()

    # 1) Known timezone aniqlash va strip
    tz_match = re.search(r'\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST)\b', s, re.IGNORECASE)
    utc_offset = TIMEZONE_UTC_OFFSET.get(tz_match.group(1).lower(), -8) if tz_match else -8
    s = re.sub(r'\s*\b(EDT|EST|CDT|CST|MDT|MST|PDT|PST)\b\s*', ' ', s, flags=re.IGNORECASE)

    # 2) "a.m." / "p.m." → "AM"/"PM"
    s = re.sub(r'([AaPp])\s*\.\s*([Mm])\s*\.?', r'\1\2', s)

    # 3) Nuqta/tire separator → colon: "09.00" / "09-00" → "09:00"
    s = re.sub(r'(\d{1,2})[.\-](\d{2})', r'\1:\2', s)

    # 4) AM/PM oldiga bo'sh joy: "04:50PM" → "04:50 PM"
    s = re.sub(r'(\d)\s*([AaPp][Mm])\b', r'\1 \2', s)

    # 5) Military (4 raqam) + AM/PM: "1000 AM" → "10:00 AM"
    s = re.sub(r'\b(\d{2})(\d{2})(\s+[AaPp][Mm])\b', r'\1:\2\3', s)

    # 6) Sof military (AM/PM yo'q): "1455" → "14:55"
    s = re.sub(r'\b(\d{2})(\d{2})\b(?!\s*[:AaPp])', r'\1:\2', s)

    # 7) Noma'lum 2-4 harfli trailing suffix (AM/PM emas) — strip: "...PM DPT" → "...PM"
    s = re.sub(r'\s+(?!(?:AM|PM)\b)[A-Za-z]{2,4}\s*$', '', s, flags=re.IGNORECASE)

    # 8) Extra bo'sh joy va trailing punktuatsiyani tozalash + uppercase
    s = re.sub(r'\s+', ' ', s).strip(" .,;:!?").upper()

    has_ampm = bool(re.search(r'[AP]M', s))
    formats = ("%I:%M %p", "%I:%M%p") if has_ampm else ("%H:%M", "%I:%M")

    parsed = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        print(f"⚠️ parse_time_to_iso: parse bo'lmadi | original={original!r} | normalized={s!r}")
        return fallback

    today_dt = datetime.now(timezone.utc)
    parsed = parsed.replace(year=today_dt.year, month=today_dt.month, day=today_dt.day)
    local_tz = timezone(timedelta(hours=utc_offset))
    local_dt = parsed.replace(tzinfo=local_tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def extract_timezone(time_str: str) -> int:
    """Vaqt stringidan timezone enum qiymatini olish. Default: PST (3)"""
    match = re.search(r'(EDT|EST|CDT|CST|MDT|MST|PDT|PST)', time_str, re.IGNORECASE)
    if match:
        return TIMEZONE_MAP.get(match.group(1).lower(), 3)
    return 3


def parse_checkin_checkout(text: str) -> dict | None:
    """BOL/POD raqami, checkin va checkout vaqtlarini parse qiladi."""
    match = CHECKIN_CHECKOUT_PATTERN.search(text)
    if not match:
        return None
    return {
        "doc_type": match.group(1).upper(),  # "BOL" yoki "POD"
        "load_id": match.group(2),
        "checkin": match.group(3).strip(),
        "checkout": match.group(4).strip(),
    }


def extract_doc_numbers_from_text(text: str) -> str:
    """Matndan BOL/POD/Load raqamlarini prefix bilan chiqarib oladi.
    LLM/regex pattern ga doc_type context berish uchun prefix ("BOL"/"POD"/"Load") saqlanadi.
    Agar prefix bo'lmasa, standalone 5+ raqamli tokenlar "Load <n>" sifatida qaytariladi
    (reply faqat raqamdan iborat bo'lgan holatlar uchun)."""
    prefixed = re.findall(
        r'(?:BOL|POD|Load(?:\s*Id)?|Load\s*#?)\s*[:#]?\s*[\w\-]+',
        text, re.IGNORECASE
    )
    if prefixed:
        return " ".join(m.strip() for m in prefixed).strip()
    standalone = re.findall(r'\b\d{5,}\b', text)
    if standalone:
        return " ".join(f"Load {n}" for n in standalone[:3])
    return ""


CHECKIN_CHECKOUT_KEYWORDS = re.compile(
    # Faqat driver harakati qilingan (verb) formalarni match qilamiz.
    # Noun'lar ("Pickup/Delivery" label, "delivery number") dispatch xabarlarida
    # uchragani uchun bilan hisobga olmaslik kerak.
    r'check(?:ed|ing)?[\s\-_]*(?:in|out)'
    r'|deliver(?:ed|ing)'                                    # "delivery" alone skipped
    r'|delivery\s+(?:complet(?:e|ed)|done|finished)'
    r'|picked[\s\-]*up|picking[\s\-]*up'                     # "pickup" alone skipped
    r'|pickup\s+(?:complet(?:e|ed)|done|finished)'
    r'|dropped[\s\-]*off|dropping[\s\-]*off'
    r'|drop\s*off\s+(?:complet(?:e|ed)|done|finished)'
    r'|loaded|unloaded'
    r'|arrived|departed'
    r'|completed\s+(?:pickup|delivery)'
    r'|load\s+(?:is\s+|has\s+been\s+)?finish(?:ed)?'
    r'|the\s+load\s+(?:is\s+|has\s+been\s+)?finish(?:ed)?'
    r'|load\s+(?:is\s+|has\s+been\s+)?complet(?:e|ed)'
    r'|finish(?:ed)?\s+(?:load|unload)ing',
    re.IGNORECASE
)


def build_checkin_checkout_text(msg: types.Message, primary_text: str) -> str:
    """Current text + reply xabaridan BOL/POD/Load raqamlarini birlashtiradi.
    Asosiy xabarda checkin/checkout kalit so'z bo'lmasa reply dan hech narsa olinmaydi.
    Load raqami OLDIN qo'yiladi, keyin checkin/checkout matni — shunday qilib
    regex pattern (load→check in→check out tartibini kutadi) ishlaydi."""
    base_text = (primary_text or "").strip()
    parts = []

    # Reply dan load raqami (prefix bilan) — faqat asosiy matnda checkin/checkout bo'lsa
    if base_text and CHECKIN_CHECKOUT_KEYWORDS.search(base_text):
        reply = getattr(msg, "reply_to_message", None)
        if reply:
            reply_text = ((getattr(reply, "text", None) or getattr(reply, "caption", None) or "")).strip()
            if reply_text and reply_text != base_text:
                doc_numbers = extract_doc_numbers_from_text(reply_text)
                if doc_numbers:
                    parts.append(doc_numbers)  # "BOL 12345" — regex'dan oldin kerak

    if base_text:
        parts.append(base_text)

    return "\n".join(parts).strip()


async def parse_checkin_checkout_llm(text: str) -> dict | None:
    """Regex topalmasa Groq LLM orqali checkin/checkout parse qiladi."""
    try:
        prompt = f"""Extract check-in/check-out info from this message. Return ONLY valid JSON, nothing else.

Message: "{text}"

Return format:
{{"doc_type": "BOL" or "POD", "load_id": "load number", "checkin": "time with timezone", "checkout": "time with timezone"}}

Rules:
- doc_type must be BOL or POD (uppercase)
  * BOL = pickup stop (e.g. "picked up", "loaded", "pick up", "pickup completed", "successfully picked up")
  * POD = delivery stop (e.g. "delivered", "delivery completed", "successfully delivered", "unloaded", "dropped off", "load finished", "the load finished", "load is finished")
  * If the message explicitly says BOL or POD, use that directly
  * If neither keyword, infer from context: pickup-related = BOL, delivery-related = POD
- load_id is the load/BOL/POD/reference number
- checkin and checkout must include time and timezone if present (e.g. "10:00 AM PST")
- If timezone is missing, keep time as-is
- If any field is missing, return null

If this is NOT a check-in/check-out or pickup/delivery confirmation message, return: null"""

        res = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a logistics data extractor. Return only valid JSON or null."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=150,
            temperature=0.1
        )
        content = res.choices[0].message.content.strip()
        if content.lower() == "null" or not content:
            return None
        # JSON ni tozalash (ba'zida ```json ... ``` ichida kelishi mumkin)
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content, flags=re.MULTILINE).strip()
        data = json.loads(content)
        if not data or not isinstance(data, dict):
            return None
        # Majburiy fieldlarni tekshirish
        if not all(k in data for k in ("doc_type", "load_id", "checkin", "checkout")):
            return None
        if data["doc_type"] not in ("BOL", "POD"):
            return None
        print(f"🤖 Groq parsed checkin/checkout: {data}")
        return data
    except Exception as e:
        await send_error_to_group(f"❌ Groq checkin/checkout parse error: {e}")
        return None


async def resolve_load_id(group_id, load_number):
    """Load number ni load ID ga aylantirish. Avval cache dan, keyin API dan qidiradi."""
    # 1. Cache da bormi tekshirish (cache da load_id sifatida saqlangan)
    load_data = await get_load_from_cache(group_id, load_number)
    if load_data:
        return str(load_number)

    # 2. API dan loadlarni olish va loadNumber bo'yicha qidirish
    try:
        loads = await get_loads_from_api(str(group_id))
        for load in loads:
            load_num = str(load.get("loadNumber", ""))
            load_id = str(load.get("id") or load.get("loadId") or "")
            # loadNumber yoki id bo'yicha mos kelsa
            if load_num == str(load_number) or load_id == str(load_number):
                print(f"🔍 Load resolved: number={load_number} -> id={load_id}")
                return load_id
    except Exception as e:
        print(f"⚠️ Could not resolve load ID: {e}")

    # Topilmasa o'zini qaytarish
    return str(load_number)


async def send_checkin_checkout(group_id, load_id, checkin, checkout, is_pickup):
    """Checkin/checkout vaqtlarini API ga yuborish. Returns dict: {success, error}"""
    print(f"📋 CheckinCheckout | Group: {group_id} | Load: {load_id} | In: {checkin} | Out: {checkout} | isPickup: {is_pickup}")

    # Load number ni haqiqiy load ID ga aylantirish
    resolved_id = await resolve_load_id(group_id, load_id)
    print(f"🔍 Resolved load ID: {load_id} -> {resolved_id}")

    token = await get_api_token()
    if not token:
        print("❌ CheckinCheckout: API token olishda xato")
        return {"success": False, "error": "API token olishda xato"}

    payload = {
        "groupId": str(group_id),
        "loadId": resolved_id,
        "checkIn": parse_time_to_iso(checkin),
        "checkOut": parse_time_to_iso(checkout),
        "timeZone": extract_timezone(checkin),
        "isPickup": is_pickup,
    }
    print(f"📤 CheckinCheckout payload: {json.dumps(payload)}")

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            headers = {"Authorization": f"Bearer {token}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}
            async with session.post(CHECKIN_CHECKOUT_URL, json=payload, headers=headers, timeout=10) as resp:
                if resp.status in (200, 201, 204):
                    print(f"✅ CheckinCheckout sent for load {load_id}")
                    return {"success": True}
                elif resp.status == 401:
                    # Token expired - yangilash va qayta urinish
                    print("🔄 Token expired, refreshing...")
                    global ACCESS_TOKEN
                    ACCESS_TOKEN = None
                    new_token = await get_api_token()
                    if new_token:
                        headers = {"Authorization": f"Bearer {new_token}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}
                        async with session.post(CHECKIN_CHECKOUT_URL, json=payload, headers=headers, timeout=10) as retry_resp:
                            if retry_resp.status in (200, 201, 204):
                                print(f"✅ CheckinCheckout sent for load {load_id} (after token refresh)")
                                return {"success": True}
                            text = await retry_resp.text()
                            await send_error_to_group(f"❌ CheckinCheckout failed after refresh: {retry_resp.status} - {text}", group_id=group_id)
                            parsed_error = None
                            try:
                                err_json = json.loads(text)
                                if isinstance(err_json.get("errors"), dict):
                                    parsed_error = (
                                        err_json["errors"].get("description")
                                        or err_json["errors"].get("message")
                                    )
                                parsed_error = parsed_error or err_json.get("description") or err_json.get("detail")
                            except Exception:
                                pass
                            return {
                                "success": False,
                                "status": retry_resp.status,
                                "error": parsed_error or f"{retry_resp.status}: {text[:200]}"
                            }
                    return {"success": False, "error": "Token refresh failed"}
                else:
                    text = await resp.text()
                    await send_error_to_group(f"❌ CheckinCheckout failed: {resp.status} - {text}", group_id=group_id)
                    parsed_error = None
                    try:
                        err_json = json.loads(text)
                        if isinstance(err_json.get("errors"), dict):
                            parsed_error = (
                                err_json["errors"].get("description")
                                or err_json["errors"].get("message")
                            )
                        parsed_error = parsed_error or err_json.get("description") or err_json.get("detail")
                    except Exception:
                        pass
                    return {
                        "success": False,
                        "status": resp.status,
                        "error": parsed_error or f"{resp.status}: {text[:200]}"
                    }
    except Exception as e:
        await send_error_to_group(f"❌ CheckinCheckout error: {e}", group_id=group_id)
        return {"success": False, "error": str(e)[:200], "server_down": True}


DISPATCH_MESSAGE_PATTERN = re.compile(
    r'(?:rate\s*\$|stop\s*\d+[\.\:]?\s*(?:pickup|delivery|live\s*load)|'
    r'\d+\s*miles|\bshipper\b|\breceiver\b|customer\s*ref\s*#|hazmat|'
    r'stop\s*1\b)',
    re.IGNORECASE
)


async def process_checkin_checkout_text(text: str, chat_id: int, msg) -> bool:
    """Text dan checkin/checkout parse qilib API ga yuborish. True = topildi."""
    print(f"🔍 process_checkin_checkout_text | chat={chat_id} | text={repr(text[:200])}")
    # Dispatch/load assignment xabarlari checkin/checkout emas — lekin checkin/checkout
    # kalit so'zlari bo'lsa ustun (masalan "...finished... Receiver: (SAN JOSE)")
    if DISPATCH_MESSAGE_PATTERN.search(text) and not CHECKIN_CHECKOUT_KEYWORDS.search(text):
        print(f"🔍 Dispatch message detected, skipping checkin/checkout")
        return False
    parsed = parse_checkin_checkout(text)
    regex_matched = parsed is not None
    print(f"🔍 regex parse result: {parsed}")
    if not parsed and CHECKIN_CHECKOUT_KEYWORDS.search(text):
        parsed = await parse_checkin_checkout_llm(text)
        print(f"🔍 llm parse result: {parsed}")
    if not parsed:
        print(f"🔍 No checkin/checkout found in text")
        return False

    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("checkInCheckOut", True):
            return True  # topildi, lekin permission o'chirilgan

    if not parsed.get("load_id"):
        # Regex match bo'lsa YOKI LLM ham checkin ham checkout topgan bo'lsa — load number so'raymiz.
        # LLM match bo'lib checkin/checkout ikkalasi ham yo'q bo'lsa — oddiy suhbat deb hisoblaymiz.
        if regex_matched or (parsed.get("checkin") and parsed.get("checkout")):
            await msg.answer("Please include the load number in your message.")
            return True
        print(f"🔍 LLM matched but checkin/checkout incomplete — treating as non-checkin message")
        return False

    is_pickup = parsed["doc_type"] == "BOL"
    result = await send_checkin_checkout(chat_id, parsed["load_id"], parsed["checkin"], parsed["checkout"], is_pickup)
    if result.get("success"):
        doc_type = parsed["doc_type"]
        stop_label = "Picked up" if doc_type == "BOL" else "Finished/delivered"
        await msg.answer(f"✅ {doc_type} Check-in/out received for Load #{parsed['load_id']} ({stop_label})")
    elif result.get("server_down") or result.get("status") in (502, 503, 504):
        print(f"⚠️ Server down — silently skipping checkin/checkout reply for Load #{parsed['load_id']}: {result.get('error')}")
    elif result.get("status") == 400:
        await msg.answer(f"❌ Invalid data for Load #{parsed['load_id']}. Please check the format and try again.")
    elif result.get("status") == 404:
        await msg.answer(f"❌ Load #{parsed['load_id']} not found. Please check the load number.")
    elif result.get("status") == 409:
        await msg.answer(result.get("error", "Conflict"))
    elif result.get("status") == 500:
        await msg.answer(f"❌ Server error. Please try again in a few minutes.")
    else:
        error = result.get("error", "Unknown error")
        await msg.answer(f"❌ Check-in/out failed for Load #{parsed['load_id']}\n\n⚠️ {error}")
    return True


# ======================================================
# === Get API Token ===
# ======================================================
async def get_api_token():
    global ACCESS_TOKEN, TOKEN_EXPIRES_AT
    # Token mavjud va eskirishiga 5 daqiqadan ko'p vaqt qolgan bo'lsa qaytarish
    if ACCESS_TOKEN and time.time() < TOKEN_EXPIRES_AT - 300:
        return ACCESS_TOKEN
    # Yangi token olish
    ACCESS_TOKEN = None
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(LOGIN_URL, json=LOGIN_CREDENTIALS, timeout=10) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    print("❌ Login failed:", resp.status, text)
                    return None
                data = await resp.json()
                ACCESS_TOKEN = data.get("accessToken")
                if ACCESS_TOKEN:
                    TOKEN_EXPIRES_AT = _decode_token_exp(ACCESS_TOKEN)
                    if TOKEN_EXPIRES_AT == 0:
                        # JWT exp o'qib bo'lmasa, 55 daqiqa default
                        TOKEN_EXPIRES_AT = time.time() + 3300
                    print(f"✅ Token acquired! Expires at: {datetime.fromtimestamp(TOKEN_EXPIRES_AT)}")
                return ACCESS_TOKEN
    except Exception as e:
        print("❌ Token acquisition error:", e)
        return None


# ======================================================
# === BOL CHECK API SECTION ===
# ======================================================

def fix_image_orientation(img_bytes: bytes) -> bytes:
    """Rasmni to'g'ri holatga keltirish"""
    try:
        img = Image.open(BytesIO(img_bytes))
        if hasattr(img, '_getexif') and img._getexif():
            exif = img._getexif()
            if exif and 274 in exif:
                orientation = exif[274]
                if orientation == 3:
                    img = img.rotate(180, expand=True)
                elif orientation == 6:
                    img = img.rotate(270, expand=True)
                elif orientation == 8:
                    img = img.rotate(90, expand=True)
        width, height = img.size
        if width > height * 1.3:
            img = img.rotate(90, expand=True)
        output = BytesIO()
        img.save(output, format='PNG')
        return output.getvalue()
    except Exception:
        return img_bytes


def pdf_to_images(pdf_bytes: bytes) -> list:
    """PDF ning barcha sahifalarini rasmlarga aylantirish"""
    images = []
    pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_num in range(min(len(pdf_doc), 5)):  # Max 5 sahifa
        page = pdf_doc.load_page(page_num)
        mat = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        mode = "RGBA" if pix.alpha else "RGB"
        pil_img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
        buf = BytesIO()
        pil_img.save(buf, format="PNG")
        img_bytes = buf.getvalue()
        img_bytes = fix_image_orientation(img_bytes)
        images.append(img_bytes)
    pdf_doc.close()
    return images


async def process_file(file_bytes: bytes, filename: str) -> list:
    """Faylni rasmga aylantirish"""
    if filename.lower().endswith('.pdf'):
        return pdf_to_images(file_bytes)
    else:
        img_bytes = fix_image_orientation(file_bytes)
        return [img_bytes]


def _parse_gemini_json(raw: str) -> dict:
    """Gemini javobidan JSON ajratib olish"""
    raw = raw.strip()
    if '```json' in raw:
        raw = raw.split('```json')[1].split('```')[0].strip()
    elif '```' in raw:
        raw = raw.split('```')[1].split('```')[0].strip()
    if not raw.startswith('{'):
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start != -1 and end > start:
            raw = raw[start:end]
    return json.loads(raw)


async def _gemini_extract_once(pil_images: list, text_prompt: str, attempt_num: int) -> dict:
    """Gemini dan bir marta BOL ma'lumot olish"""
    # PIL Image → bytes Part (yangi SDK PIL ni qabul qilmaydi)
    image_parts = []
    for img in pil_images:
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_parts.append(genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

    for retry in range(3):
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: genai_client.models.generate_content(
                model=GEMINI_BOT_MODEL,
                contents=[text_prompt] + image_parts,
                config=genai_types.GenerateContentConfig(max_output_tokens=10000)
            )
        )
        result = response.text.strip()
        print(f"🤖 Gemini attempt {attempt_num} raw response: {result[:500]}")
        try:
            return _parse_gemini_json(result)
        except json.JSONDecodeError as e:
            print(f"⚠️ Gemini JSON parse error (attempt {attempt_num}, retry {retry+1}): {e} | raw len={len(result)}")
            if retry == 2:
                raise
            await asyncio.sleep(1)
    raise ValueError("Gemini JSON parse failed after retries")


def _merge_bol_results(r1: dict, r2: dict) -> dict:
    """
    Ikki natijani solishtirish va birlashtirish.
    Mos kelgan field → qabul qilinadi.
    Mos kelmaganlar → r3 (tiebreaker) uchun qaytariladi.
    """
    KEY_FIELDS = ["pickup", "delivery", "weight", "signature", "po_number"]
    merged = {}
    conflicts = []

    for key in KEY_FIELDS:
        v1 = r1.get(key, "not found")
        v2 = r2.get(key, "not found")
        if v1 == v2:
            merged[key] = v1
        elif v1 == "not found":
            merged[key] = v2
        elif v2 == "not found":
            merged[key] = v1
        else:
            # Ikkalasi ham bor lekin farq qiladi
            conflicts.append(key)
            merged[key] = v1  # vaqtincha r1 dan olish

    # Qolgan fieldlar (pallets, seal_number) — r1 dan
    for key in ["pallets", "seal_number"]:
        merged[key] = r1.get(key, r2.get(key, "not found"))

    return merged, conflicts


async def analyze_bol_detailed(images_list: list) -> dict:
    """BOL ni Gemini 2.5 Pro bilan 2 marta tahlil qilish (self-consistency)"""
    print(f"🖼️ analyze_bol_detailed: {len(images_list)} ta rasm keldi, sizes: {[len(img) for img in images_list]}")

    # bytes → PIL Image
    pil_images = [Image.open(BytesIO(img_bytes)) for img_bytes in images_list]

    text_prompt = """You are an expert at extracting information from freight shipping documents.

This document may have ANY of these titles — treat all of them as a BOL (Bill of Lading):
- "Bill of Lading" / "BOL"
- "Contract Route Vehicle Record"
- "PS Form 5398-A"
- "USPS Contract Route Vehicle Record"
- Any similar shipping or freight document

Extract ALL the following information:

1. PICKUP: Shipper/origin facility name and full address
2. DELIVERY: Consignee/destination facility name and full address
3. WEIGHT: Total weight (in lbs or kg)
4. SIGNATURE: Is there a driver signature? Look for any handwritten signature
5. PO_NUMBER: Purchase Order number, PRO number, BOL number, Trip number, Route No, or any reference number
6. PALLETS: Number of pallets/pieces
7. SEAL_NUMBER: Seal number if present

CRITICAL FOR "Contract Route Vehicle Record" / PS Form 5398-A:
- "Dispatch Fac" or "Dispatch. Fac" = PICKUP location (e.g., "OAKLAND (CA) P&DC" means Oakland, CA)
- "Destination" = DELIVERY location (e.g., "07Z-NJI (NJ) NDC" means New Jersey)
- "Route No" or "Trip" = PO_NUMBER
- The document may be rotated 90 degrees - read it carefully!
- Look for fields: Trip, Route No, Dep (departure), Arr (arrival), Van No, Etracs

USPS ZONE CODE MEANINGS:
- 07Z = New Jersey area
- 98Z = Seattle, WA metro area
- 9EZ = Federal Way, WA (Seattle P&DC)
- DEN = Denver, CO area
- P&DC = Processing & Distribution Center
- NDC = Network Distribution Center

IMPORTANT: If you see "Dispatch Fac: OAKLAND (CA)" - pickup is "Oakland, CA"
IMPORTANT: If you see "Destination: 07Z-NJI (NJ)" - delivery is "07Z-NJI, NJ" or "New Jersey"
NEVER return "not found" if you see Dispatch Fac or Destination fields!

Return ONLY a valid JSON object:
{
    "pickup": "FACILITY NAME, ADDRESS, CITY, STATE ZIP",
    "delivery": "FACILITY NAME, ADDRESS, CITY, STATE ZIP",
    "weight": "weight value with unit (e.g., 26000 lbs)",
    "signature": "yes" or "no",
    "po_number": "PO or BOL number or Trip number or Route No",
    "pallets": "number of pallets",
    "seal_number": "seal number or not found"
}

If any field is not found, use "not found" for that field."""

    tiebreaker_prompt = """You are an expert at reading freight shipping documents (BOL).
Look at this document carefully ONE MORE TIME and extract ONLY these specific fields.
Be extra careful and thorough.

Return ONLY a valid JSON object:
{
    "pickup": "FACILITY NAME, ADDRESS, CITY, STATE ZIP",
    "delivery": "FACILITY NAME, ADDRESS, CITY, STATE ZIP",
    "weight": "weight value with unit",
    "signature": "yes" or "no",
    "po_number": "reference/BOL/PO number"
}

If any field is not found, use "not found"."""

    try:
        # 1-chi va 2-chi o'qish parallel
        r1, r2 = await asyncio.gather(
            _gemini_extract_once(pil_images, text_prompt, 1),
            _gemini_extract_once(pil_images, text_prompt, 2),
        )
        print(f"🔁 Attempt 1: pickup={r1.get('pickup','?')[:60]}, delivery={r1.get('delivery','?')[:60]}")
        print(f"🔁 Attempt 2: pickup={r2.get('pickup','?')[:60]}, delivery={r2.get('delivery','?')[:60]}")

        merged, conflicts = _merge_bol_results(r1, r2)

        if conflicts:
            print(f"⚠️ Conflicts in fields {conflicts}, running tiebreaker (attempt 3)...")
            r3 = await _gemini_extract_once(pil_images, tiebreaker_prompt, 3)
            for key in conflicts:
                v3 = r3.get(key, "not found")
                if v3 != "not found":
                    merged[key] = v3
                    print(f"✅ Tiebreaker resolved '{key}': {v3[:80]}")
        else:
            print(f"✅ All key fields consistent across 2 attempts")

        print(f"✅ Final merged result: pickup={merged.get('pickup','?')[:60]}, delivery={merged.get('delivery','?')[:60]}")
        return merged

    except Exception as e:
        await send_error_to_group(f"❌ analyze_bol_detailed exception: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


async def get_loads_from_api(group_id: str) -> list:
    """API dan guruh uchun load'larni olish"""
    global ACCESS_TOKEN
    # Token olish
    token = await get_api_token()
    if not token:
        raise HTTPException(status_code=401, detail="API login failed - token olib bo'lmadi")

    url = f"{BASE_URL}/paperwork-issues/load-ratecon/groups/{group_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Accept-Language": "EN",
        "X-Group-Id": str(group_id)
    }

    async with httpx.AsyncClient() as client_http:
        response = await client_http.get(url, headers=headers)
        if response.status_code == 200:
            loads = response.json()
            # Debug: birinchi load ni log qilish
            if loads and len(loads) > 0:
                first_load = loads[0]
                print(f"📋 API dan olingan birinchi load keys: {list(first_load.keys())}")
                print(f"📋 Birinchi load sample: loadId={first_load.get('loadId')}, id={first_load.get('id')}")
            return loads
        else:
            raise HTTPException(status_code=response.status_code, detail=f"API xatosi: {response.text}")


def count_stops_by_type(load: dict) -> tuple:
    """Load dan pickup va delivery sonini hisoblash"""
    stops = load.get("stops", [])
    pickup_count = 0
    delivery_count = 0

    for stop in stops:
        stop_type = stop.get("type", "").lower()
        if stop_type == "pickup":
            pickup_count += 1
        elif stop_type == "delivery":
            delivery_count += 1

    # Minimum 1 ta bo'lishi kerak
    return max(pickup_count, 1), max(delivery_count, 1)


def determine_file_type(stops: list) -> int:
    """
    API'dan kelgan stops asosida fayl turini aniqlash.
    1  → BOL (hali to'lmagan pickup bor)
    2  → POD (barcha pickup to'lgan, hali to'lmagan delivery bor)
    -1 → Hammasi to'lgan, hujjat kerak emas
    0  → Stops yo'q, aniqlash mumkin emas
    """
    if not stops:
        return 0
    pickups    = [s for s in stops if s.get("type", "").lower() == "pickup"]
    deliveries = [s for s in stops if s.get("type", "").lower() == "delivery"]
    if any(not s.get("isComplete", False) for s in pickups):
        return 1
    if deliveries and any(not s.get("isComplete", False) for s in deliveries):
        return 2
    return -1


async def get_load_stop_counts(group_id: str, load_id: str) -> tuple:
    """Load uchun pickup va delivery sonini API dan olish"""
    try:
        loads = await get_loads_from_api(group_id)
        for load in loads:
            lid = str(load.get("id") or load.get("loadId") or "")
            if lid == str(load_id):
                return count_stops_by_type(load)
        # Load topilmasa default
        return 1, 1
    except Exception as e:
        print(f"⚠️ Could not get stop counts: {e}")
        return 1, 1


def format_ratecon_address(stop: dict) -> str:
    """RateCon stop'dan address stringini yaratish"""
    addr = stop.get("address", {})
    parts = []
    if addr.get("address_line_1"):
        parts.append(addr["address_line_1"])
    if addr.get("address_line_2"):
        parts.append(addr["address_line_2"])
    if addr.get("city"):
        parts.append(addr["city"])
    if addr.get("state"):
        parts.append(addr["state"])
    if addr.get("zip"):
        parts.append(addr["zip"])
    return ", ".join(parts) if parts else "not found"


async def find_matching_load(bol_data: dict, loads: list) -> tuple:
    """BOL ni qaysi load'ga tegishli ekanini aniqlash"""

    bol_pickup = bol_data.get("pickup", "not found")
    bol_delivery = bol_data.get("delivery", "not found")

    best_match = None
    best_score = 0

    for load in loads:
        stops = load.get("stops", [])

        # Pickup va delivery ni ajratib olish
        ratecon_pickup = ""
        ratecon_delivery = ""

        for stop in stops:
            if stop.get("type") == "pickup":
                ratecon_pickup = format_ratecon_address(stop)
            elif stop.get("type") == "delivery":
                ratecon_delivery = format_ratecon_address(stop)

        # Solishtirish
        pickup_match, _ = await semantic_compare(ratecon_pickup, bol_pickup, "pickup")
        delivery_match, _ = await semantic_compare(ratecon_delivery, bol_delivery, "delivery")

        score = (1 if pickup_match else 0) + (1 if delivery_match else 0)

        if score > best_score:
            best_score = score
            best_match = load

        # Ikkalasi ham mos kelsa, to'xtaymiz
        if score == 2:
            break

    return best_match, best_score


def compare_weight(bol_weight: str, ratecon_weight: str) -> dict:
    """Weight'larni solishtirish"""
    if bol_weight == "not found":
        return {"isHealthy": False, "summary": "Weight NOT FOUND on BOL!"}
    if not ratecon_weight:
        return {"isHealthy": True, "summary": f"BOL: {bol_weight} (No weight on RateCon)"}

    # Raqamlarni ajratib olish
    import re
    bol_num = re.findall(r'[\d,]+', bol_weight.replace(',', ''))
    ratecon_num = re.findall(r'[\d,]+', str(ratecon_weight).replace(',', ''))

    if bol_num and ratecon_num:
        bol_val = int(bol_num[0].replace(',', ''))
        ratecon_val = int(ratecon_num[0].replace(',', ''))

        # 10% farq ruxsat etiladi
        diff_percent = abs(bol_val - ratecon_val) / max(bol_val, ratecon_val) * 100

        if diff_percent <= 10:
            return {"isHealthy": True, "summary": f"BOL: {bol_weight}, RateCon: {ratecon_weight} - MATCH"}
        else:
            return {"isHealthy": False,
                    "summary": f"BOL: {bol_weight}, RateCon: {ratecon_weight} - DIFF {diff_percent:.1f}%"}

    return {"isHealthy": True, "summary": f"BOL: {bol_weight}, RateCon: {ratecon_weight}"}


def check_signature(signature: str) -> dict:
    """Signature borligini tekshirish"""
    if signature.lower() == "yes":
        return {"isHealthy": True, "summary": "Signature found ✓"}
    else:
        return {"isHealthy": False, "summary": "Signature NOT FOUND!"}


def check_po_number(bol_po: str) -> dict:
    """PO number borligini tekshirish"""
    if bol_po and bol_po != "not found":
        return {"isHealthy": True, "summary": f"PO/BOL#: {bol_po}"}
    else:
        return {"isHealthy": False, "summary": "PO number NOT FOUND"}


def get_pdf_page_count(file_bytes: bytes, file_name: str) -> int:
    """PDF yoki rasm fayldan sahifa sonini olish"""
    try:
        # PDF fayl
        if file_name.lower().endswith('.pdf'):
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            page_count = len(pdf_doc)
            pdf_doc.close()
            return page_count
        else:
            # Rasm fayl - 1 sahifa
            return 1
    except Exception as e:
        print(f"⚠️ Page count olishda xato: {e}")
        return 1


async def post_paperwork_issue(result_data: dict, bol_file_bytes: bytes, file_name: str, file_type: int = 1, group_id: int = None) -> dict:
    """Natijani POST /api/paperwork-issues ga multipart form sifatida yuborish. file_type: 1=BOL, 2=POD"""
    global ACCESS_TOKEN
    token = await get_api_token()
    if not token:
        return {"success": False, "error": "Token olib bo'lmadi"}

    url = f"{BASE_URL}/v1/paperwork-issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Accept-Language": "EN"
    }
    if group_id:
        headers["X-Group-Id"] = str(group_id)

    try:
        # PDF sahifa sonini olish
        page_count = get_pdf_page_count(bol_file_bytes, file_name)

        # Form data tayyorlash
        files = {
            "File": (file_name, bol_file_bytes, "application/octet-stream")
        }

        data = {
            "LoadId": result_data["loadId"],
            "FileType": file_type,  # 1=Bol, 2=Pod
            "Weight.IsHealthy": str(result_data["weight"]["isHealthy"]).lower(),
            "Weight.Summary": result_data["weight"]["summary"],
            "Signature.IsHealthy": str(result_data["signature"]["isHealthy"]).lower(),
            "Signature.Summary": result_data["signature"]["summary"],
            "PoNumber.IsHealthy": str(result_data["poNumber"]["isHealthy"]).lower(),
            "PoNumber.Summary": result_data["poNumber"]["summary"],
            "PickUpAddress.IsHealthy": str(result_data["pickUpAddress"]["isHealthy"]).lower(),
            "PickUpAddress.Summary": result_data["pickUpAddress"]["summary"],
            "DeliveryAddressAddress.IsHealthy": str(result_data["deliveryAddressAddress"]["isHealthy"]).lower(),
            "DeliveryAddressAddress.Summary": result_data["deliveryAddressAddress"]["summary"],
            "PageCount.IsHealthy": "true",
            "PageCount.Summary": str(page_count),
            "RouteNumber.IsHealthy": "true",
            "RouteNumber.Summary": result_data.get("routeNumber", {}).get("summary", "N/A"),
            "TripNumber.IsHealthy": "true",
            "TripNumber.Summary": result_data.get("tripNumber", {}).get("summary", "N/A"),
            "SealNumber.IsHealthy": "true",
            "SealNumber.Summary": result_data.get("sealNumber", {}).get("summary", "N/A"),
        }

        # DEBUG: Yuborilayotgan ma'lumotni log qilish
        print(f"📋 Paperwork API ga yuborilayotgan data:")
        print(f"   URL: {url}")
        print(f"   Token: {token[:50]}...")
        for key, value in data.items():
            print(f"   {key}: {value}")
        print(f"   File: {file_name} ({len(bol_file_bytes)} bytes)")

        async with httpx.AsyncClient(timeout=30) as client_http:
            response = await client_http.post(url, data=data, files=files, headers=headers)
            print(f"📨 Paperwork API response: {response.status_code}")
            print(f"📨 Response body: {response.text[:500] if response.text else 'empty'}")
            if response.status_code in [200, 201]:
                return {"success": True, "response": response.json() if response.text else {}}
            else:
                return {"success": False, "error": f"Status: {response.status_code}, Response: {response.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def validate_bol_with_loads_gemini(bol_images: list, loads: list) -> dict:
    """BOL rasmlarni RateCon JSON bilan solishtirish (self-consistency: 2 parallel call + tiebreaker)."""
    loads_info = []
    for i, load in enumerate(loads):
        stops = load.get("stops", [])
        pickup_addrs = [format_ratecon_address(s) for s in stops if s.get("type") == "pickup"]
        delivery_addrs = [format_ratecon_address(s) for s in stops if s.get("type") == "delivery"]
        loads_info.append({
            "index": i + 1,
            "loadId": load.get("loadId"),
            "dbId": load.get("id"),
            "loadNumber": load.get("loadNumber"),
            "pickup": pickup_addrs,
            "delivery": delivery_addrs,
            "weight": load.get("weight", ""),
        })

    loads_json = json.dumps(loads_info, indent=2)

    prompt = f"""You are a logistics document verification expert. Carefully analyze the freight shipping document in the image.

AVAILABLE LOADS FROM SYSTEM:
{loads_json}

STEP 1 — Determine document type (set "isBOL" true/false):
ACCEPT as BOL (isBOL=true):
  - "Bill of Lading", "BOL", "Straight Bill of Lading"
  - "Contract Route Vehicle Record", "PS Form 5398-A", "USPS Contract Route Vehicle Record"
  - Any pickup document signed by SHIPPER or DRIVER at origin
REJECT as NOT BOL (isBOL=false):
  - Rate Confirmation / Rate Con (shows pay rates, broker/carrier info, lanes)
  - Invoice or Freight Invoice
  - Proof of Delivery / POD (signed by RECEIVER at destination)
  - Scale Ticket, Lumper Receipt, Detention form
  - Blank template with no data filled in

STEP 2 — Find matching load (set "matchedIndex" to 1-based index, or 0 if no match):
Match signals ranked by strength:
  1. LOAD/BOL NUMBER (strongest): Does the document's BOL#, PO#, Pro#, Trip#, Route#, or reference number match any load's "loadNumber"?
  2. PICKUP CITY/STATE: Does the shipper/origin/dispatch facility match a load's pickup address?
  3. DELIVERY CITY/STATE: Does the consignee/destination match a load's delivery address?
  4. WEIGHT: Use as secondary confirmation only
USPS zone codes: 98Z=Seattle WA, 9EZ=Federal Way WA, DEN=Denver CO, 07Z=New Jersey NJ, P&DC=Processing Center, NDC=Network Distribution Center
Same city or metro area counts as a match even if street address differs.
If a load's loadNumber appears anywhere on the document — that is a strong match.
Set matchedIndex=0 only if NO load matches on ANY signal.

STEP 3 — Validate matched load fields:
1. WEIGHT: Is weight present and readable? Compare with load weight if available.
2. SIGNATURE: Is there a handwritten driver/shipper signature visible?
3. PO NUMBER: Is there a BOL#/PO#/Pro#/Trip#/Route# present on the document?
4. PICKUP ADDRESS: Does document's origin/shipper match the matched load's pickup?
5. DELIVERY ADDRESS: Does document's destination/consignee match the matched load's delivery?

Return ONLY valid JSON — no markdown, no code fences, no extra text:
{{
    "isBOL": true or false,
    "matchedIndex": 0,
    "weight": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "signature": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "poNumber": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "pickUpAddress": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "deliveryAddressAddress": {{"isHealthy": true or false, "summary": "brief explanation"}}
}}"""

    # Her bir call uchun alohida PIL image ochiladi (shared stream muammosini oldini olish)
    pil_images_1 = [Image.open(BytesIO(img)) for img in bol_images]
    pil_images_2 = [Image.open(BytesIO(img)) for img in bol_images]

    # Self-consistency: 2 parallel Gemini calls
    r1, r2 = await asyncio.gather(
        _gemini_extract_once(pil_images_1, prompt, 1),
        _gemini_extract_once(pil_images_2, prompt, 2),
    )
    print(f"🔁 Validate attempt 1: isBOL={r1.get('isBOL')}, matchedIndex={r1.get('matchedIndex')}")
    print(f"🔁 Validate attempt 2: isBOL={r2.get('isBOL')}, matchedIndex={r2.get('matchedIndex')}")

    if r1.get("isBOL") == r2.get("isBOL") and r1.get("matchedIndex") == r2.get("matchedIndex"):
        print(f"✅ Both attempts agree: isBOL={r1.get('isBOL')}, matchedIndex={r1.get('matchedIndex')}")
        return r1

    print(f"⚠️ Disagreement between attempts, running tiebreaker (attempt 3)...")
    pil_images_3 = [Image.open(BytesIO(img)) for img in bol_images]
    r3 = await _gemini_extract_once(pil_images_3, prompt, 3)
    print(f"🔁 Tiebreaker result: isBOL={r3.get('isBOL')}, matchedIndex={r3.get('matchedIndex')}")
    return r3


# === BOL Check Endpoint ===
@app.post("/check-bol")
async def check_bol_endpoint(
        group_id: str = Form(..., description="Telegram group ID (masalan: -5043574387)"),
        bol: UploadFile = File(..., description="BOL fayl (PDF yoki rasm)")
):
    try:
        bol_file_bytes = await bol.read()
        bol_file_name = bol.filename or "bol_document"

        # 1. BOL ni rasmlarga aylantirish
        print(f"📄 BOL tahlil qilinmoqda...")
        bol_images = await process_file(bol_file_bytes, bol_file_name)

        # 2. API dan loadlarni olish
        print(f"📥 Group {group_id} uchun loadlar olinmoqda...")
        try:
            loads = await get_loads_from_api(group_id)
        except HTTPException as e:
            return {"success": False, "message": f"Loadlarni olishda xato: {e.detail}"}

        if not loads:
            return {"success": False, "message": "Bu guruh uchun hech qanday load topilmadi",
                    "bol_data": {"pickup": "not found", "delivery": "not found"}}

        print(f"✅ {len(loads)} ta load topildi")

        # 3. BOL + RateCon JSON → bitta Gemini call
        print(f"🔍 BOL ni loadlar bilan Gemini orqali solishtirmoqda...")
        result = await validate_bol_with_loads_gemini(bol_images, loads)
        print(f"✅ Gemini natija: isBOL={result.get('isBOL')}, matchedIndex={result.get('matchedIndex')}")

        if not result.get("isBOL", False):
            return {"success": False, "message": "Document is not a BOL",
                    "bol_data": {"pickup": "not found", "delivery": "not found"}}

        matched_index = result.get("matchedIndex", 0)
        if not matched_index or matched_index < 1 or matched_index > len(loads):
            return {"success": False, "message": "BOL did not match any load",
                    "bol_data": {"pickup": "not found", "delivery": "not found"},
                    "available_loads": len(loads)}

        matched_load = loads[matched_index - 1]
        _raw_load_number = matched_load.get("loadId")
        _raw_load_db_id = matched_load.get("id")
        try:
            load_number = int(_raw_load_number) if _raw_load_number is not None else None
        except (ValueError, TypeError):
            load_number = _raw_load_number
        try:
            load_db_id = int(_raw_load_db_id) if _raw_load_db_id is not None else None
        except (ValueError, TypeError):
            load_db_id = _raw_load_db_id
        load_id = load_db_id if load_db_id is not None else load_number
        stops = matched_load.get("stops", [])

        print(f"✅ Load #{load_number} (db_id={load_db_id}) ga mos keldi")

        paperwork_data = {
            "loadId": load_id,
            "weight": result.get("weight", {"isHealthy": False, "summary": "Not checked"}),
            "signature": result.get("signature", {"isHealthy": False, "summary": "Not checked"}),
            "poNumber": result.get("poNumber", {"isHealthy": False, "summary": "Not checked"}),
            "pickUpAddress": result.get("pickUpAddress", {"isHealthy": False, "summary": "Not checked"}),
            "deliveryAddressAddress": result.get("deliveryAddressAddress", {"isHealthy": False, "summary": "Not checked"}),
        }

        return {
            "success": True,
            "loadId": load_number,
            "loadDbId": load_db_id,
            "message": "BOL verified successfully",
            "paperwork_result": paperwork_data,
            "stops": stops
        }

    except Exception as e:
        await send_error_to_group(f"❌ check-bol xatolik: {e}", group_id=chat_id)
        import traceback; traceback.print_exc()
        return {"success": False, "message": f"Kutilmagan xatolik: {str(e)}"}


# ======================================================
# === Telegram Bot Functions (continue) ===
# ======================================================

async def check_driver_cooldown(driver_id):
    """Driverning oxirgi ticket yuborgan vaqti bilan solishtiramiz"""
    now = datetime.now()
    last_time = DRIVER_COOLDOWN.get(driver_id)

    if last_time is None:
        return True, None  # Cooldown yo'q

    time_diff = (now - last_time).total_seconds()
    if time_diff < COOLDOWN_DURATION:
        remaining = COOLDOWN_DURATION - time_diff
        return False, remaining  # Cooldown davom etmoqda

    return True, None  # Cooldown tugagan


async def update_driver_cooldown(driver_id):
    """Driverning oxirgi ticket vaqtini yangilaymiz"""
    DRIVER_COOLDOWN[driver_id] = datetime.now()


async def check_conversation_timeout(group_id, user_id):
    """Conversation bo'lsa, oxirgi conversation vaqtini tekshiramiz (FAQAT NON-DRIVER UCHUN)"""
    # Agar driver bo'lsa, conversation timeout o'chiriladi
    if is_any_driver(group_id, user_id):
        return True  # Driver uchun hech qanday kutish yo'q

    # Faqat non-driverlar uchun conversation timeout
    now = datetime.now()
    last_conv_time = CONVERSATION_LAST_TIME.get(group_id)

    if last_conv_time is None:
        return True  # Conversation yo'q, ishlash mumkin

    time_diff = (now - last_conv_time).total_seconds()
    if time_diff < CONVERSATION_TIMEOUT:
        remaining = CONVERSATION_TIMEOUT - time_diff
        print(f"⏳ Conversation davom etmoqda (non-driver). {int(remaining)} soniya kutish kerak.")
        return False  # Conversation davom etmoqda

    return True  # Conversation tugagan


async def update_conversation_time(group_id, user_id):
    """Conversation bo'lganda vaqtni yangilaymiz (FAQAT NON-DRIVER UCHUN)"""
    # Faqat non-driverlar uchun conversation vaqtini yangilaymiz
    if not is_any_driver(group_id, user_id):
        CONVERSATION_LAST_TIME[group_id] = datetime.now()
        print(f"💬 Conversation time updated for non-driver {user_id}")


async def check_ticket_similarity(new_ticket_text, group_id):
    """AI yordamida yangi ticket avvalgi ticketlarga o'xshashligini tekshiramiz"""
    if group_id not in LAST_TICKETS:
        LAST_TICKETS[group_id] = new_ticket_text
        return False  # Birinchi ticket, o'xshashlik yo'q

    last_ticket = LAST_TICKETS[group_id]

    try:
        prompt = f"""
        Compare these two messages and determine if they are about the SAME ISSUE.
        Reply with ONLY ONE WORD: "yes" or "no".

        Consider them as the same issue if:
        1. They describe the same problem or request
        2. They have the same main topic
        3. They are likely duplicates
        4. The second one is just a rewording of the first

        If they are different problems, reply "no".

        Previous message: "{last_ticket}"
        New message: "{new_ticket_text}"

        Are they the same issue? Reply only "yes" or "no":
        """

        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system",
                 "content": "Compare if two messages are about the same issue. Reply only with 'yes' or 'no'."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=5,
            temperature=0.1
        )

        similarity_result = res.choices[0].message.content.strip().lower()
        is_similar = similarity_result == "yes"

        if is_similar:
            print(f"⚠️ AI detected similar tickets. Not sending new ticket.")
            return True
        else:
            # Agar o'xshash bo'lmasa, yangi ticketni saqlaymiz
            LAST_TICKETS[group_id] = new_ticket_text
            return False

    except Exception as e:
        await send_error_to_group(f"❌ AI similarity check error: {e}")
        # Xatolik bo'lsa, o'xshash deb hisoblamaymiz
        LAST_TICKETS[group_id] = new_ticket_text
        return False


async def detect_priority(text: str):
    """AI yordamida xabar prioritetini aniqlash"""
    try:
        prompt = f"""
        Analyze this message and determine its priority level for a logistics company.
        Reply with ONLY ONE WORD: "high", "medium", or "low".

        HIGH priority: accidents, safety hazards, urgent delivery problems, critical vehicle breakdowns, emergencies
        MEDIUM priority: maintenance requests, delivery delays, payment issues, HR inquiries
        LOW priority: general questions, non-urgent updates, casual conversation

        Message: "{text}"
        Priority:
        """
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "Determine priority level. Reply with only one word."},
                      {"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.1
        )
        priority = res.choices[0].message.content.strip().lower()
        if priority in ["high", "medium", "low"]:
            return priority
        return "medium"  # default
    except Exception as e:
        print("❌ Priority detection error:", e)
        return "medium"


async def send_action_log(group_id, message):
    global ACCESS_TOKEN
    if not ACCESS_TOKEN:
        await get_api_token()

    def wrap_first_word(text):
        pattern = r'^(\w+)(.*)$'
        match = re.match(pattern, text)
        if match:
            first_word = match.group(1)
            rest = match.group(2) if match.group(2) else ""
            return f"<mark>{first_word}</mark>{rest}"
        return text

    wrapped_message = wrap_first_word(message)
    payload = {
        "createdAt": datetime.now().isoformat() + "Z",
        "message": f"{wrapped_message}",
        "groupId": str(group_id)
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept-Language": "EN",
        "Content-Type": "application/json",
        "X-Group-Id": str(group_id)
    }

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(ACTION_LOGS_URL, json=payload, headers=headers, timeout=10) as resp:
                response_text = await resp.text()
                if resp.status in (200, 201):
                    print(f"✅ Action log sent successfully for group {group_id}")
                    return True
                else:
                    print(f"⚠️ Action log error [{resp.status}]: {response_text}")
                    return False
    except Exception as e:
        print("❌ Error sending action log:", e)
        return False


async def get_quickbuttons(group_id):
    global ACCESS_TOKEN
    if not ACCESS_TOKEN:
        await get_api_token()

    group_id_str = str(group_id)
    if group_id_str.startswith('-'):
        if group_id_str.startswith('-100'):
            group_id_str = group_id_str[0:]
        else:
            group_id_str = group_id_str[0:]

    url = f"{BASE_URL}/quick-action-buttons/by-group/{group_id_str}"
    params = {"PageIndex": 1, "PageSize": 50}
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    await send_error_to_group(f"❌ Quickbuttons API error: {resp.status}", group_id=group_id_str)
                    return DEFAULT_QUICK_BUTTONS

                data = await resp.json()
                buttons = [x["title"] for x in data.get("items", [])]
                if not buttons:
                    return DEFAULT_QUICK_BUTTONS

                print(f"✅ Loaded {len(buttons)} quick buttons from API for group {group_id_str}")
                return buttons

    except asyncio.TimeoutError:
        await send_error_to_group(f"❌ Quickbuttons API timeout, using defaults", group_id=group_id_str)
        return DEFAULT_QUICK_BUTTONS
    except Exception as e:
        await send_error_to_group(f"❌ Error fetching quickbuttons: {e}", group_id=group_id_str)
        return DEFAULT_QUICK_BUTTONS


def build_quickbuttons_keyboard(buttons: list):
    kb = [[KeyboardButton(text=btn)] for btn in buttons]
    kb.append([KeyboardButton(text="🔄 Refresh")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


async def create_message_link(msg: types.Message):
    try:
        chat_id = msg.chat.id
        message_id = msg.message_id

        if msg.chat.type == "private":
            return f"private:{chat_id}:{message_id}"
        elif msg.chat.type in ["group", "supergroup"]:
            if str(chat_id).startswith('-100'):
                group_id = str(chat_id).replace('-100', '')
            elif str(chat_id).startswith('-'):
                group_id = str(chat_id).replace('-', '')
            else:
                group_id = str(chat_id)
            message_link = f"https://t.me/c/{group_id}/{message_id}"
            return message_link
        else:
            return f"unknown:{chat_id}:{message_id}"
    except Exception as e:
        await send_error_to_group(f"❌ Error creating message link: {e}")
        return f"error:{msg.message_id}"


async def send_to_swagger(groupId, groupName, writerName, writerId, department, text, message_link, priority="high", ticket_type=0):
    """Ticket yuborish, agar error bo'lsa qayta registratsiya qilish"""
    global ACCESS_TOKEN

    if not ACCESS_TOKEN:
        await get_api_token()

    payload = {
        "messageId": str(message_link),
        "groupId": str(groupId),
        "groupName": groupName,
        "writerName": writerName,
        "writerId": str(writerId),
        "priority": PRIORITY_MAP.get(priority, 0),
        "department": DEPARTMENT_MAP.get(department, 1),
        "status": STATUS_TODO,
        "type": ticket_type,
        "assignedTo": "",
        "text": text,
        "attachments": []
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Accept-Language": "EN",
        "Content-Type": "application/json",
        "X-Group-Id": str(groupId)
    }

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(SWAGGER_URL, json=payload, headers=headers, timeout=10) as resp:
                response_text = await resp.text()

                # Muvaffaqiyatli yuborildi
                if resp.status in (200, 201):
                    print(f"✅ Ticket sent successfully to {department} with priority {priority}")

                    # Group ticket status'ini "todo" qilish
                    group_id_str = str(groupId)
                    GROUP_TICKET_STATUS[group_id_str] = {
                        "status": "todo",
                        "created_at": datetime.now().isoformat(),
                        "done_at": None
                    }
                    print(f"📋 Group {groupId} ticket status set to 'todo'")

                    # Ticket statusni groups_token_cache.json ga saqlash
                    save_group_ticket_status(groupId, "todo")

                    # Polling taskni start qilish (agar mavjud bo'lmasa)
                    if group_id_str not in GROUP_TICKET_POLLING_TASKS:
                        polling_task = asyncio.create_task(poll_backend_ticket_status(group_id_str))
                        GROUP_TICKET_POLLING_TASKS[group_id_str] = polling_task
                        print(f"🔄 Started polling task for group {groupId}")

                    # Groupga xabar chiqarish (agar kerak bo'lsa)
                    if GROUP_TICKET_NOTIFICATIONS and department != "chat":
                        notification_text = f"📋 ✅ Ticket successfully sent to {department}"
                        try:
                            await bot.send_message(chat_id=groupId, text=notification_text)
                        except:
                            pass

                    return True, response_text

                # 404 - DriverNotAssignedToGroup xatosi
                elif resp.status == 404:
                    print(f"⚠️ Swagger error [404]: {response_text}")
                    # Bu registratsiya muammosi emas - driver tayinlanmagan
                    # Registratsiyani tozalamaslik kerak
                    return False, f"Driver not assigned to group: {response_text}"

                # Session expired xatolari
                elif resp.status in (401, 403):
                    print(f"⚠️ Swagger error [{resp.status}]: {response_text}")
                    ACCESS_TOKEN = None
                    await get_api_token()
                    return False, f"Session expired: {response_text}"

                # Boshqa xatolar
                elif resp.status == 400:
                    print(f"⚠️ Swagger error [400]: {response_text}")
                    # Bad request - registratsiyani tozalamaslik kerak
                    return False, f"Bad request: {response_text}"

                # Boshqa status kodlar
                else:
                    await send_error_to_group(f"❌ Swagger error [{resp.status}]: {response_text}", group_id=group_id)
                    return False, response_text

    except Exception as e:
        print("❌ Error sending ticket:", e)
        return False, str(e)


async def check_group_registered(group_id, group_name):
    """Guruh ro'yxatdan o'tganligini tekshirish - avval by-group API, keyin fayldan token"""
    global ACCESS_TOKEN

    # Group ID ni str ga o'tkazish (consistency uchun)
    group_id_str = str(group_id)

    # 1. RAM cache tekshirish (tez) - int va str ikkalasini ham tekshirish
    if REGISTERED_GROUPS.get(group_id) or REGISTERED_GROUPS.get(group_id_str):
        print(f"ℹ️ Group {group_id} found in RAM cache (registered)")
        return True

    # 2. API access token olish
    if not ACCESS_TOKEN:
        await get_api_token()

    # 3. /api/group-links/{groupId}/by-group orqali tekshirish
    group_link_url = f"{BASE_URL}/group-links/{group_id}/by-group"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept-Language": "EN", "X-Group-Id": str(group_id)}

    print(f"🔍 Checking group via by-group API: {group_id}")

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.get(group_link_url, headers=headers, timeout=10) as resp:
                response_text = await resp.text()
                print(f"🔍 by-group API response: status={resp.status}")

                if resp.status == 200:
                    # Guruh topildi - registratsiya qilingan
                    print(f"✅ Group {group_id} is registered (by-group API)")
                    REGISTERED_GROUPS[group_id] = True
                    REGISTERED_GROUPS[group_id_str] = True  # str versiyasi ham
                    AWAITING_TOKEN.pop(group_id, None)
                    AWAITING_TOKEN.pop(group_id_str, None)
                    # by-group javobidan companyId va driver holatini tekshirish
                    try:
                        resp_data = json.loads(response_text)
                        company_id = resp_data.get("companyId") or resp_data.get("company_id")
                        if company_id:
                            save_group_company_id(group_id, company_id)
                        # driverId va driverName ni tekshirish
                        driver_id_backend = resp_data.get("driverId")
                        driver_name_backend = resp_data.get("driverName", "").strip()
                        if driver_id_backend is None:
                            # Backend da driver o'chirilgan — local dan ham o'chirish
                            local_driver = GROUP_DRIVER_IDS.pop(group_id_str, None)
                            if local_driver:
                                print(f"🗑️ Driver removed from local (driverId=null in backend) for group {group_id_str}")
                                async with aiosqlite.connect(DB_PATH) as db:
                                    await db.execute(
                                        "UPDATE groups SET driver_id=NULL, driver_name=NULL, updated_at=datetime('now') WHERE group_id=?",
                                        (group_id_str,)
                                    )
                                    await db.commit()
                        elif driver_name_backend:
                            # Backend da driver bor — ismini yangilash
                            local_driver = GROUP_DRIVER_IDS.get(group_id_str)
                            if local_driver:
                                async with aiosqlite.connect(DB_PATH) as db:
                                    await db.execute(
                                        "UPDATE groups SET driver_name=?, updated_at=datetime('now') WHERE group_id=?",
                                        (driver_name_backend, group_id_str)
                                    )
                                    await db.commit()
                                print(f"🔄 Driver name updated from backend: '{driver_name_backend}' for group {group_id_str}")
                    except Exception as e:
                        print(f"⚠️ by-group parse error: {e}")
                    return True

                elif resp.status == 404:
                    # Guruh API da not found
                    await send_error_to_group(f"❌ Group not found in by-group API", group_id=group_id)
                    # Fayldagi tokenni tekshirmaymiz - chunki validate_bot_token qaytadan registratsiya qiladi
                    # Faqat token so'raymiz
                    REGISTERED_GROUPS.pop(group_id, None)
                    REGISTERED_GROUPS.pop(group_id_str, None)
                    AWAITING_TOKEN[group_id] = True
                    return False

                elif resp.status in (401, 403):
                    await send_error_to_group(f"❌ API token expired, refreshing...")
                    ACCESS_TOKEN = None
                    await get_api_token()
                    AWAITING_TOKEN[group_id] = True
                    return False

                else:
                    print(f"⚠️ Unexpected response [{resp.status}]: {response_text}")
                    return None  # Server error (500/502/etc) - retry kerak

    except Exception as e:
        await send_error_to_group(f"❌ Error checking group registration: {e}", group_id=group_id)
        return None  # Connection error - retry kerak


async def check_group_registered_force(group_id, group_name, force_check=False):
    """Group registratsiyasini majburiy tekshirish (cache'dan o'tmay)"""
    global ACCESS_TOKEN

    group_id_str = str(group_id)

    # Agar force_check False bo'lsa va cache da bo'lsa - API ga so'rov yubormaslik
    if not force_check:
        if REGISTERED_GROUPS.get(group_id) or REGISTERED_GROUPS.get(group_id_str):
            print(f"ℹ️ Group {group_id} found in RAM cache (skipping API call)")
            return True

    # Agar force_check True bo'lsa, cache'ni tozalaymiz
    if force_check:
        REGISTERED_GROUPS.pop(group_id, None)
        REGISTERED_GROUPS.pop(group_id_str, None)
        print(f"🧹 Cleared cache for group {group_id}")

    # Endi API orqali tekshirish
    return await check_group_registered(group_id, group_name)


async def wait_for_server_and_check(group_id, group_name, msg, force_check=False) -> bool:
    """500/502 yoki connection error bo'lsa 'Connecting...' xabar chiqarib retry qiladi."""
    result = await check_group_registered_force(group_id, group_name, force_check=force_check)
    if result is not None:
        return result

    try:
        while True:
            await asyncio.sleep(5)
            result = await check_group_registered_force(group_id, group_name, force_check=force_check)
            if result is not None:
                return result
    except Exception as e:
        await send_error_to_group(f"❌ wait_for_server_and_check error: {e}", group_id=group_id)
        return False


async def validate_bot_token(access_token, user_token, group_id, group_name):
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            payload = {
                "token": user_token,
                "groupId": str(group_id),
                "groupName": group_name
            }
            headers = {
                "Accept-Language": "EN",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Group-Id": str(group_id)
            }

            print(f"🔐 Validating token for group {group_id}...")
            async with session.post(VALIDATE_TOKEN_URL, json=payload, headers=headers, timeout=10) as resp:
                response_text = await resp.text()
                print(f"📨 Validation response status: {resp.status}")
                try:
                    data = await resp.json()
                    return {
                        "success": resp.status == 200,
                        "status": resp.status,
                        "data": data,
                        "message": data.get("message", "Validation completed")
                    }
                except:
                    return {
                        "success": resp.status == 200,
                        "status": resp.status,
                        "data": None,
                        "message": response_text,
                        "raw_response": response_text
                    }
    except Exception as e:
        print("❌ Validate Token Error:", e)
        return {
            "success": False,
            "error": str(e),
            "message": f"Validation error: {str(e)}"
        }


async def summarize_text(text: str):
    if not text:
        return text
    if len(text.split()) <= 6:
        return text
    try:
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "Summarize briefly but keep meaning."},
                      {"role": "user", "content": text}],
            max_tokens=60,
            temperature=0.3
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Summarize error:", e)
        return text


async def classify_message(text: str):
    try:
        prompt = f"""
        Classify this message into ONE of these categories ONLY:
        - "chat": for greetings, small talk, casual conversation (hi, hello, good morning, how are you, etc.)
        - "dispatcher": for dispatch-related issues, routing, delivery problems
        - "fleet": for vehicle issues, maintenance, fleet management
        - "safety": for safety concerns, accidents, violations
        - "insurance": for insurance claims, coverage questions
        - "hr": for human resources, employment issues, hiring, termination
        - "accounting": for billing, invoices, financial matters, salary, payroll, money, payment problems
        - "updater": for system updates, technical issues, basket-related issues

        If the message contains "basket", or mentions any fruits or vegetables (e.g. apple, tomato, potato, onion, carrot, meva, sabzavot, olma, pomidor, kartoshka etc.), classify as "updater".
        Only reply with one word from the categories above.

        Message: "{text}"
        """
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system",
                       "content": "Classify logistics messages precisely. Reply with only one word from the specified categories."},
                      {"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.1
        )
        classification = res.choices[0].message.content.strip().lower()
        valid_categories = ["chat", "dispatcher", "fleet", "safety", "insurance", "hr", "accounting", "updater"]
        return classification if classification in valid_categories else "updater"
    except Exception as e:
        print("❌ Classify error:", e)
        return "updater"


async def semantic_compare(ratecon_addr: str, bol_addr: str, addr_type: str) -> tuple:
    """GPT orqali ikki manzilni semantik taqqoslash"""

    if ratecon_addr == "not found" or bol_addr == "not found":
        return False, "Ma'lumot not found"

    prompt = f"""Compare these two {addr_type} locations to see if they refer to the SAME AREA/REGION.

RATECON: {ratecon_addr}

BOL: {bol_addr}

IMPORTANT - USPS ZONE CODE MATCHING:
- USPS zone codes represent REGIONS, not exact addresses
- 98Z-SEATTLE = Seattle metro area (includes Federal Way, Tacoma, Kent, Auburn, Bellevue, etc.)
- 9EZ = Seattle P&DC = Federal Way, WA area
- If BOL shows "98Z-SEATTLE" and RateCon shows any address in Seattle metro area = MATCH
- If BOL shows "98Z-SEATTLE" and RateCon shows "Federal Way, WA" = MATCH
- Denver Transfer Hub = DEN zone

IGNORE COMPLETELY:
- Company/facility names
- Street numbers (they can be different!)
- Building/unit numbers

ANSWER "MATCH: YES" if:
- Same city/metro area (Federal Way is in Seattle metro = MATCH)
- Same region/zone (98Z includes all Seattle area cities)
- Same street name with abbreviations (AVE=Avenue, ST=Street, RD=Road, S=SOUTH)
- Different street NUMBERS are OK (704 vs 708 = still MATCH)

ANSWER "MATCH: NO" ONLY if:
- Completely different cities/regions (e.g., Denver vs Seattle = NO MATCH)
- Not in the same metro area

Answer EXACTLY:
MATCH: YES or NO
REASON: Brief explanation"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.1
        )

        result = response.choices[0].message.content.strip()
        is_match = "MATCH: YES" in result.upper()
        return is_match, result
    except Exception as e:
        return False, f"Taqqoslash xatosi: {e}"


# ======================================================
# === Telegram Handlers ===
# ======================================================

@dp.my_chat_member()
async def on_my_chat_member(event: types.ChatMemberUpdated):
    """Bot guruhdan chiqarilsa yoki guruh o'chirilsa — local ma'lumotlarni tozalash"""
    new_status = event.new_chat_member.status
    if new_status not in ("kicked", "left", "banned"):
        return

    group_id = event.chat.id
    group_id_str = str(group_id)
    print(f"🗑️ Bot removed from group {group_id} (status={new_status}), cleaning up...")

    # JSON cache dan o'chirish
    remove_group_token(group_id)

    # RAM cache lardan o'chirish
    REGISTERED_GROUPS.pop(group_id, None)
    REGISTERED_GROUPS.pop(group_id_str, None)
    AWAITING_TOKEN.pop(group_id, None)
    AWAITING_TOKEN.pop(group_id_str, None)
    GROUP_DRIVER_IDS.pop(group_id_str, None)
    if group_id in STARTED_GROUPS:
        STARTED_GROUPS.discard(group_id)
        save_started_groups()

    # SQLite DB dan o'chirish
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM groups WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM loads WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM bols WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM pods WHERE group_id=?", (group_id_str,))
        await db.commit()

    print(f"✅ Cleanup done for group {group_id}")


@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    chat_name = msg.chat.title or msg.from_user.full_name or "Private Chat"

    await msg.answer("🔎 Checking group registration... Please wait.")
    access_token = await get_api_token()
    if not access_token:
        await msg.answer("❌ API connection failed. Please try again later.")
        return

    # MAJBURIY tekshirish (cache'ni tozalab)
    registered = await check_group_registered_force(chat_id, chat_name, force_check=True)

    if registered:
        AWAITING_TOKEN.pop(chat_id, None)
        TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)
        mark_group_started(chat_id)
        await msg.answer("✅ Group is registered! Bot is ready to use.")
        await send_action_log(chat_id, f"Group checked: {chat_name}")

        # Quick buttons faqat driver uchun
        if is_any_driver(chat_id, msg.from_user.id):
            buttons = await get_quickbuttons(chat_id)
            keyboard = build_quickbuttons_keyboard(buttons)
            await msg.answer("Quick buttons loaded:", reply_markup=keyboard)
        else:
            # Driver bo'lmaganlar uchun "I am a driver" tugmasi
            kb = InlineKeyboardBuilder()
            kb.button(text="👤 I am a driver", callback_data="set_driver")
            await msg.answer("Bot is ready to use. Quick buttons are only available for drivers.",
                             reply_markup=kb.as_markup())
    else:
        # Agar registratsiya testi o'tmagan bo'lsa, token so'raymiz
        # Faqat /start bosgan user dan token qabul qilamiz
        AWAITING_TOKEN[chat_id] = msg.from_user.id
        TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)
        await msg.answer("❌ Group is not registered. Please send the admin token (single line).")


@dp.callback_query(F.data == "set_driver")
async def set_driver_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    driver_id = callback.from_user.id
    chat_name = callback.message.chat.title or callback.from_user.full_name or "Private Chat"

    # Avval guruh tekshiruvi (by-group orqali companyId sync)
    registered = await wait_for_server_and_check(chat_id, chat_name, callback.message, force_check=True)
    if not registered:
        await callback.answer("❌ Group is not registered.", show_alert=True)
        await callback.message.answer("❌ Group is not registered. Please use /start to register first.")
        return

    # Agar allaqachon driver bo'lsa, e'tiborsiz qoldirish
    if is_any_driver(chat_id, driver_id):
        await callback.answer("✅ You are already set as a driver.", show_alert=False)
        return

    primary = get_group_driver(chat_id)
    if primary is None:
        # Birinchi driver → asosiy driver
        await save_driver_id(chat_id, driver_id, callback.from_user.full_name)
        await send_action_log(chat_id, f"Driver set: {callback.from_user.full_name}")
        await callback.answer("✅ You are now set as a driver.", show_alert=False)
    elif get_team_driver(chat_id) is None:
        # Ikkinchi driver → team driver
        await save_team_driver_id(chat_id, driver_id, callback.from_user.full_name)
        await send_action_log(chat_id, f"Team driver set: {callback.from_user.full_name}")
        await callback.answer("✅ You are now set as a team driver.", show_alert=False)
    else:
        await callback.answer("❌ This group already has 2 drivers. Use /teamdriver to replace.", show_alert=True)
        return

    buttons = await get_quickbuttons(chat_id)
    keyboard = build_quickbuttons_keyboard(buttons)
    await callback.message.answer("Quick buttons loaded:", reply_markup=keyboard)


# ======================================================
# === /help Command ===
# ======================================================
@dp.message(Command("help"))
async def help_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)
    await msg.answer(
        "📋 <b>Available Commands</b>\n\n"
        "/start — Register the group and load quick buttons\n"
        "/setdriver — Set the primary driver for this group\n"
        "  • Reply to driver's message → /setdriver\n"
        "  • By ID → /setdriver 123456789\n"
        "/teamdriver — Set the team (second) driver for this group\n"
        "  • Reply to driver's message → /teamdriver\n"
        "  • By ID → /teamdriver 123456789\n"
        "  • Remove → /teamdriver remove\n"
        "/sleep — Set driver rest time (1, 2, 4, 6, 8 hours)\n"
        "/deletesleep — Delete driver's active sleep timer\n"
        "/help — Show this help message\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📄 <b>DOCUMENTS</b>\n\n"
        "Send a photo or file (PDF, image) of your BOL or POD.\n"
        "The bot will automatically convert to PDF.\n"
        "Then click Analyze button to check BOL/POD\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📍 <b>CHECK-IN / CHECKOUT</b>\n\n"
        "Send your check-in or checkout time as a text message.\n\n"
        "<blockquote>POD 2320961\n\n"
        "The load has been delivered\n\n"
        "Check in: 06:28 EDT\n"
        "Check out: 08:30 EDT</blockquote>\n\n"
        "The bot will parse the time and update the load check-in/out time.",
        parse_mode="HTML"
    )


# ======================================================
# === /setdriver Command ===
# ======================================================


@dp.message(Command("setdriver"))
async def setdriver_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    group_id_str = str(chat_id)

    # 1. Reply orqali — reply qilingan xabarning egasi driver bo'ladi
    if msg.reply_to_message:
        driver_user = msg.reply_to_message.from_user
        if driver_user.is_bot:
            await msg.answer("⛔ Cannot set a bot as driver.")
            return
        new_driver_id = driver_user.id
        driver_name = driver_user.full_name
        GROUP_DRIVER_IDS[group_id_str] = new_driver_id
        await save_driver_id(chat_id, new_driver_id, driver_name)
        await send_action_log(chat_id, f"Driver set via reply: {driver_name} (ID: {new_driver_id})")

    # 2. ID orqali — /setdriver 123456789
    elif len(msg.text.split()) > 1:
        try:
            new_driver_id = int(msg.text.split()[1])
        except ValueError:
            await msg.answer("⛔ Invalid ID. Usage:\n/setdriver 123456789\nor reply to driver's message with /setdriver")
            return
        GROUP_DRIVER_IDS[group_id_str] = new_driver_id
        await save_driver_id(chat_id, new_driver_id, str(new_driver_id))
        await send_action_log(chat_id, f"Driver set via ID: {new_driver_id}")

    else:
        await msg.answer(
            "Usage:\n"
            "1. Reply to driver's message → /setdriver\n"
            "2. Direct ID → /setdriver 123456789"
        )
        return

    # by-group orqali companyId sync
    chat_name = msg.chat.title or msg.from_user.full_name or "Group"
    await check_group_registered_force(chat_id, chat_name, force_check=True)

    await msg.answer("✅ Driver set successfully.")


# ======================================================
# === /teamdriver Command ===
# ======================================================
@dp.message(Command("teamdriver"))
async def teamdriver_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    group_id_str = str(chat_id)

    # Asosiy driver bo'lmasa, team driver qo'shib bo'lmaydi
    if get_group_driver(chat_id) is None:
        await msg.answer("⛔ No primary driver set. Use /setdriver first.")
        return

    args = msg.text.split()

    # /teamdriver remove — team driverni o'chirish
    if len(args) > 1 and args[1].lower() == "remove":
        if get_team_driver(chat_id) is None:
            await msg.answer("ℹ️ No team driver to remove.")
        else:
            await remove_team_driver(chat_id)
            await send_action_log(chat_id, "Team driver removed")
            await msg.answer("✅ Team driver removed.")
        return

    # Reply orqali
    if msg.reply_to_message:
        driver_user = msg.reply_to_message.from_user
        if driver_user.is_bot:
            await msg.answer("⛔ Cannot set a bot as team driver.")
            return
        if driver_user.id == get_group_driver(chat_id):
            await msg.answer("⛔ This user is already the primary driver.")
            return
        await save_team_driver_id(chat_id, driver_user.id, driver_user.full_name)
        await send_action_log(chat_id, f"Team driver set via reply: {driver_user.full_name} (ID: {driver_user.id})")

    # ID orqali
    elif len(args) > 1:
        try:
            new_team_id = int(args[1])
        except ValueError:
            await msg.answer("⛔ Invalid ID. Usage:\n/teamdriver 123456789\nor reply to driver's message with /teamdriver")
            return
        if new_team_id == get_group_driver(chat_id):
            await msg.answer("⛔ This user is already the primary driver.")
            return
        await save_team_driver_id(chat_id, new_team_id, str(new_team_id))
        await send_action_log(chat_id, f"Team driver set via ID: {new_team_id}")

    else:
        await msg.answer(
            "Usage:\n"
            "1. Reply to driver's message → /teamdriver\n"
            "2. Direct ID → /teamdriver 123456789\n"
            "3. Remove team driver → /teamdriver remove"
        )
        return

    await msg.answer("✅ Team driver set successfully.")


# ======================================================
# === /sleep Command ===
# ======================================================
@dp.message(Command("sleep"))
async def sleep_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    user_id = msg.from_user.id

    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("sleepTime", True):
            return

    kb = InlineKeyboardBuilder()
    for h in [1, 2, 4, 6, 8]:
        kb.button(text=f"🕐 {h}h", callback_data=f"sleep_{h}")
    kb.adjust(5)
    await msg.answer("😴 Select your rest time:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("sleep_"))
async def sleep_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    hours = int(callback.data.split("_")[1])

    # Backend API ga yuborish
    token = await get_api_token()
    if not token:
        await callback.message.answer("❌ Failed to get API token.")
        await callback.answer()
        return

    payload = {"groupId": str(chat_id), "sleepTimeAmountInHours": hours}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            headers = {"Authorization": f"Bearer {token}", "X-Group-Id": str(chat_id)}
            async with session.post(SLEEP_TIMER_URL, json=payload, headers=headers, timeout=10) as resp:
                if resp.status in (200, 201):
                    print(f"✅ Sleep timer set for {hours}h in group {chat_id}")
                elif resp.status == 404:
                    await callback.message.answer("To set sleep timer pls link driver to the group.")
                    await callback.answer()
                    return
                else:
                    text = await resp.text()
                    await send_error_to_group(f"❌ Sleep timer API error [{resp.status}]: {text}", group_id=callback.message.chat.id)
                    await callback.message.answer("⚠️ Driver already has an active sleep timer.")
                    await callback.answer()
                    return
    except Exception as e:
        await send_error_to_group(f"❌ Sleep timer API exception: {e}", group_id=callback.message.chat.id)
        await callback.message.answer("⚠️ Driver already has an active sleep timer.")
        await callback.answer()
        return

    await callback.message.edit_text(f"😴 Driver is resting for {hours} hour(s).")
    await callback.answer()


# ======================================================
# === /deletesleep Command ===
# ======================================================
@dp.message(Command("deletesleep"))
async def deletesleep_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    user_id = msg.from_user.id

    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("sleepTime", True):
            return

    token = await get_api_token()
    if not token:
        await msg.answer("⚠️ Authentication error. Please try again.")
        return

    payload = {"groupId": str(chat_id)}
    print(f"🗑️ Deleting sleep timer for group {chat_id}, URL: {SLEEP_TIMER_URL}, payload: {payload}")
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            headers = {"Authorization": f"Bearer {token}", "X-Group-Id": str(chat_id)}
            async with session.delete(
                SLEEP_TIMER_URL,
                data=aiohttp.FormData(fields=[("groupId", str(chat_id))]),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
                print(f"🗑️ Delete sleep timer response [{resp.status}]: {text}")
                if resp.status in (200, 201, 204):
                    await msg.answer("✅ Sleep timer deleted successfully!")
                else:
                    await msg.answer("⚠️ Failed to delete sleep timer. It may not exist.")
    except Exception as e:
        await send_error_to_group(f"❌ Delete sleep timer API exception: {e}", group_id=msg.chat.id)
        await msg.answer("⚠️ Failed to delete sleep timer.")


@dp.message(F.text)
async def generic_text_handler(msg: types.Message):
    chat_id = msg.chat.id
    chat_name = msg.chat.title or msg.from_user.full_name or "Private Chat"
    text = msg.text.strip()
    user_id = msg.from_user.id

    # /start bosilmagan guruhda bot jim turadi
    if chat_id not in STARTED_GROUPS and not AWAITING_TOKEN.get(chat_id):
        return

    # === REFRESH BUTTON (FAQAT DRIVER UCHUN) ===
    if text == "🔄 Refresh":
        if not is_any_driver(chat_id, user_id):
            return
        await send_action_log(chat_id, "Refresh button clicked")

        # MAJBURIY tekshirish
        registered = await wait_for_server_and_check(chat_id, chat_name, msg, force_check=True)
        if not registered:
            await msg.answer("❌ Group is not registered. Please use /start to re-register.")
            return

        buttons = await get_quickbuttons(chat_id)
        keyboard = build_quickbuttons_keyboard(buttons)
        await msg.answer("🔄 Buttons refreshed!", reply_markup=keyboard)
        return

    # === QUICK BUTTONS (FAQAT DRIVER UCHUN) ===
    if is_any_driver(chat_id, user_id) and text in DEFAULT_QUICK_BUTTONS:
        print(f"🔘 Quick button pressed: {text}")
        await update_conversation_time(chat_id, user_id)

    # === TOKEN RECEIVING (FAQAT /start BOSGAN USER UCHUN) ===
    if AWAITING_TOKEN.get(chat_id) and AWAITING_TOKEN[chat_id] == user_id:
        await msg.answer("🔐 Token received... validating.")
        access_token = await get_api_token()
        if not access_token:
            await msg.answer("❌ API login failed. Try again later.")
            return

        result = await validate_bot_token(access_token, text, chat_id, chat_name)
        if result.get("success"):
            # ✅ Token muvaffaqiyatli - FAYLGA SAQLASH
            save_group_token(chat_id, text, chat_name)
            AWAITING_TOKEN.pop(chat_id, None)
            TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)

            # companyId ni validation javobidan saqlash (bo'lsa)
            try:
                resp_data = result.get("data") or {}
                company_id = resp_data.get("companyId") or resp_data.get("company_id")
                if company_id:
                    save_group_company_id(chat_id, company_id)
                    print(f"💾 companyId {company_id} saved after token validation for group {chat_id}")
            except Exception:
                pass

            # by-group orqali to'liq sync (companyId + driver)
            await check_group_registered_force(chat_id, chat_name, force_check=True)

            mark_group_started(chat_id)
            await msg.answer("🎉 Group successfully registered and ready to use!")
            await send_action_log(chat_id, f"Group registration successful: {chat_name}")

            # Quick buttons faqat driver uchun
            if is_any_driver(chat_id, user_id):
                buttons = await get_quickbuttons(chat_id)
                keyboard = build_quickbuttons_keyboard(buttons)
                await msg.answer("Quick buttons loaded:", reply_markup=keyboard)
            else:
                kb = InlineKeyboardBuilder()
                kb.button(text="👤 I am a driver", callback_data="set_driver")
                await msg.answer("Bot is ready to use. Quick buttons are only available for drivers.",
                                 reply_markup=kb.as_markup())
        else:
            error_msg = result.get('message', 'Unknown error')
            if '<' in error_msg and '>' in error_msg:
                error_msg = "Server returned HTML response. Please check if the token is correct."

            attempts = TOKEN_FAILED_ATTEMPTS.get(chat_id, 0) + 1
            TOKEN_FAILED_ATTEMPTS[chat_id] = attempts

            if attempts >= 2:
                # Ikki marta xato — bot jim turadi, /start qayta bosilgunicha javob bermaydi
                AWAITING_TOKEN.pop(chat_id, None)
                TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)
            else:
                await msg.answer(
                    f"❌ Token validation failed: {error_msg}\n"
                    "Please send the correct admin token. (1 attempt left before bot stops responding.)"
                )
        return

    # === CHECKIN/CHECKOUT PARSING (TODO holatida ham ishlashi kerak) ===
    checkin_text = build_checkin_checkout_text(msg, text)
    checkin_processed = await process_checkin_checkout_text(checkin_text, chat_id, msg)

    if checkin_processed:
        return

    # === TODO holatida groupdagi har qanday yozuv history API'ga ketadi ===
    # Registratsiya tekshirishdan OLDIN — todo bo'lsa server check shart emas
    if await forward_message_to_history_if_todo(msg, fallback_text=text):
        return

    # === CHECK IF GROUP IS REGISTERED ===
    # MAJBURIY tekshirish (cache'dan o'tmay)
    registered = await wait_for_server_and_check(chat_id, chat_name, msg, force_check=False)
    if not registered:
        # Oldingi registratsiyani tozalash
        REGISTERED_GROUPS.pop(str(chat_id), None)
        # /start bosilmagunicha bot jim turadi
        return

    # === ACTION LOG ===
    await send_action_log(chat_id, f"Message from {msg.from_user.full_name}: {text[:50]}...")

    # === CLASSIFY MESSAGE ===
    try:
        dep = await classify_message(text)
    except Exception as e:
        print("❌ classify_message failed:", e)
        dep = "updater"

    # === BASKET CHECK - CHECKING + DASHBOARD ===
    if "basket" in text.lower():
        _basket_company_id = await get_or_fetch_company_id(chat_id)
        if _basket_company_id:
            _basket_perms = await get_company_permissions(str(_basket_company_id))
            if _basket_perms and not _basket_perms.get("ticketCreate", True):
                return
        print(f"🧺 Basket keyword detected - sending to dashboard")
        # Dashboard'ga yuborish
        priority = await detect_priority(text)
        text_summary = await summarize_text(text)
        message_link = await create_message_link(msg)
        ok, resp = await send_to_swagger(
            groupId=chat_id,
            groupName=chat_name,
            writerName=msg.from_user.full_name,
            writerId=msg.from_user.id,
            department="updater",
            text=text_summary,
            message_link=message_link,
            priority=priority
        )
        if ok:
            print(f"✅ Basket ticket sent to updater department")
        await msg.answer("🔍 Checking...")
        return

    # === CHAT MESSAGES ===
    if dep == "chat":
        # Darhol history API'ga yuborish
        await send_message_to_history_api(
            group_id=chat_id,
            writer_name=msg.from_user.full_name,
            message=text
        )
        print(f"💬 Chat message sent to history API for group {chat_id}")
        return  # AI javob chiqarmaymiz, ticket ham yubormaymiz

    # === NON-DRIVER MESSAGES - TICKET YUBORILMASIN ===
    if not is_any_driver(chat_id, user_id):
        print(f"⚠️ Non-driver message. User {user_id} is not a driver. Ticket will not be sent.")
        return  # Non-driver uchun ticket yuborilmaydi

    # === CHECK GROUP TICKET STATUS (PREVENT NEW TICKET IF STATUS IS 'TODO' OR 'DONE') ===
    group_id_str = str(chat_id)
    if group_id_str in GROUP_TICKET_STATUS:
        ticket_status = GROUP_TICKET_STATUS[group_id_str].get("status")
        if ticket_status == "done":
            return  # Yangi ticket yuborilmasin

    # === COOLDOWN CHECK (FAQAT DRIVER UCHUN) ===
    if is_any_driver(chat_id, user_id):
        can_send, remaining = await check_driver_cooldown(user_id)
        if not can_send:
            print(f"⏳ Driver {user_id} cooldown: {int(remaining)} seconds remaining")
            return  # Hech narsa chiqarmaymiz

    # === PROBLEM/TICKET MESSAGES (FAQAT DRIVER UCHUN) ===
    # Check ticketCreate permission
    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("ticketCreate", True):
            return
    else:
        perms = None

    # Detect priority
    priority = await detect_priority(text)
    if perms and perms.get("taskParaphrase", False):
        text_summary = await summarize_text(text)
    else:
        text_summary = text
    message_link = await create_message_link(msg)

    ok, resp = await send_to_swagger(
        groupId=chat_id,
        groupName=chat_name,
        writerName=msg.from_user.full_name,
        writerId=msg.from_user.id,
        department=dep,
        text=text_summary,
        message_link=message_link,
        priority=priority,
        ticket_type=1
    )


# ======================================================
# === BOL/POD Document Handler ===
# ======================================================

def build_pdf_from_images(pages: list) -> bytes:
    """Bir nechta rasm byteslaridan bitta PDF yasaydi"""
    imgs = []
    for page_bytes in pages:
        img = Image.open(BytesIO(page_bytes))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        imgs.append(img)
    buf = BytesIO()
    imgs[0].save(buf, format='PDF', save_all=True, append_images=imgs[1:])
    return buf.getvalue()


async def _auto_remove_analyze_button(chat_id: int, group_key: str, message_id: int):
    """Placeholder — tugma endi avtomatik o'chirilmaydi.
    5 daqiqadan keyin yangi rasm kelsa u alohida guruh sifatida ishlanadi."""
    pass


async def _send_image_prompt(chat_id: int, group_key: str, msg: types.Message):
    """1.5s debounce: hamma rasmlar yigilgandan keyin bitta prompt chiqarish."""
    await asyncio.sleep(1.5)
    pending = GROUP_PENDING_IMAGES.get(group_key)
    if not pending or not pending["pages"]:
        return

    if pending.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id, pending["prompt_msg_id"])
        except Exception:
            pass
        pending["prompt_msg_id"] = None

    page_count = len(pending["pages"])
    try:
        pdf_bytes = build_pdf_from_images(pending["pages"])
    except Exception as e:
        await msg.answer(f"❌ Image conversion failed: {e}")
        return

    pdf_name = f"doc_{msg.message_id}.pdf"
    kb = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="🔍 Analyze", callback_data="pending_bol"),
    ]])
    sent = await msg.answer_document(
        types.BufferedInputFile(pdf_bytes, filename=pdf_name),
        caption=f"Images collected and converted to {page_count} page PDF",
        reply_markup=kb
    )
    pending["prompt_msg_id"] = sent.message_id

    # Avvalgi timeout taskni bekor qilish
    existing_timeout = GROUP_IMAGE_TIMEOUT_TASKS.get(group_key)
    if existing_timeout and not existing_timeout.done():
        existing_timeout.cancel()
    GROUP_IMAGE_TIMEOUT_TASKS[group_key] = asyncio.create_task(
        _auto_remove_analyze_button(chat_id, group_key, sent.message_id)
    )


async def run_bol_check(chat_id: int, file_bytes_value: bytes, file_name: str, msg: types.Message, answer_msg=None):
    """PDF ni /check-bol ga yuborish va natijani qayta ishlash"""
    checking_msg = answer_msg or await msg.answer("🔍 Checking document...")

    files = {'bol': (file_name, file_bytes_value, 'application/octet-stream')}
    data = {'group_id': str(chat_id)}

    print(f"🔵 run_bol_check: sending POST to /check-bol...")
    try:
        async with httpx.AsyncClient() as client_http:
            response = await client_http.post(
                "http://localhost:8045/check-bol",
                data=data, files=files, timeout=180
            )
            print(f"🔵 run_bol_check: POST returned status={response.status_code}")
            if response.status_code == 422:
                await send_error_to_group(f"❌ check-bol 422 error: {response.text}", group_id=chat_id)
                await checking_msg.delete()
                await msg.answer("❌ Document check failed (invalid request). Please try again.")
                return
            print(f"🔵 HTTP {response.status_code} received, parsing JSON...")
            check_result = response.json()
            print(f"🔵 JSON parsed OK: {list(check_result.keys())}")
    except httpx.TimeoutException:
        await send_error_to_group(f"❌ run_bol_check TIMEOUT", group_id=chat_id)
        await checking_msg.delete()
        await msg.answer("⏱️ Document check timed out. Please try again.")
        return
    except Exception as e:
        await send_error_to_group(f"❌ run_bol_check EXCEPTION: {e}", group_id=chat_id)
        await checking_msg.delete()
        await msg.answer(f"❌ Document check failed: {e}")
        return

    print(f"📋 check-bol API response: success={check_result.get('success')}, message={check_result.get('message', 'N/A')[:100]}")

    if check_result.get('success'):
        _db_id = check_result.get('loadDbId')
        new_load_id = _db_id if _db_id is not None else check_result.get('loadId', 'N/A')
        load_display_id = check_result.get('loadId', new_load_id)

        stops = check_result.get('stops', [])
        file_type = determine_file_type(stops)
        print(f"📋 determine_file_type → {file_type} for load #{load_display_id}")

        # Agar stops'da pickuplar complete ko'rinsa (file_type=2) lekin hali BOL DB da yo'q bo'lsa,
        # bu birinchi hujjat — BOL sifatida qabul qilamiz (backend race condition).
        # Agar BOL allaqachon DB da bo'lsa — bu ikkinchi hujjat (POD), file_type=2 to'g'ri.
        if file_type == 2 and 'paperwork_result' in check_result:
            existing_bol = await get_last_bol(chat_id, new_load_id)
            if not existing_bol:
                print(f"⚠️ determine_file_type=2 but no BOL in DB yet → treating as BOL (file_type=1)")
                file_type = 1
            else:
                print(f"✅ BOL already in DB → keeping file_type=2 (POD)")

        pickup_count, delivery_count = count_stops_by_type({"stops": stops})
        load_data = await get_load_from_cache(chat_id, new_load_id)
        if not load_data:
            await init_load_in_cache(chat_id, new_load_id, pickup_count, delivery_count)

        if file_type == -1:
            await checking_msg.delete()
            await msg.answer(f"ℹ️ Load #{load_display_id} is already complete. No document needed.")
            return

        if file_type == 0:
            file_type = 1

        if file_type == 1:
            await add_bol_to_cache(chat_id, new_load_id, msg.message_id, file_bytes_value)
            bols_count = await get_bols_count(chat_id, new_load_id)
            required_bols = await get_pickup_count(chat_id, new_load_id)
            await checking_msg.delete()
            await msg.answer(f"✅ Load #{load_display_id} BOL #{bols_count}/{required_bols} received!")

            print(f"📋 check_result keys: {list(check_result.keys())}")
            if 'paperwork_result' in check_result:
                paperwork = check_result['paperwork_result']
                print(f"📋 paperwork loadId={paperwork.get('loadId')}, calling post_paperwork_issue...")
                bol_post_result = await post_paperwork_issue(
                    paperwork, file_bytes_value, file_name or "bol_document", file_type=1, group_id=chat_id
                )
                if bol_post_result["success"]:
                    print(f"✅ BOL paperwork issue yuborildi! Load #{new_load_id}")
                else:
                    await send_error_to_group(f"❌ BOL paperwork issue xatosi: {bol_post_result.get('error')}", group_id=chat_id)

        elif file_type == 2:
            await checking_msg.delete()
            bol_bytes = await get_last_bol(chat_id, new_load_id)
            if not bol_bytes:
                print(f"⚠️ No BOL on file for group={chat_id}, load={new_load_id}")
                return

            result = await verify_delivery(bol_bytes, file_bytes_value)
            if result.get("success"):
                await add_pod_to_cache(chat_id, new_load_id, msg.message_id, file_bytes_value)
                pods_count = await get_pods_count(chat_id, new_load_id)
                required_pods = await get_delivery_count(chat_id, new_load_id)

                verify_data = result.get("data", {})
                address_match = verify_data.get("address_match", False)
                pod_valid = verify_data.get("pod_valid", False)
                address_notes = verify_data.get("address_notes", verify_data.get("notes", ""))[:200]
                pod_notes = verify_data.get("pod_notes", verify_data.get("notes", ""))[:200]

                pod_paperwork_data = {
                    "loadId": new_load_id,
                    "weight": {"isHealthy": True, "summary": "N/A"},
                    "signature": {"isHealthy": pod_valid, "summary": pod_notes if pod_notes else ("Found" if pod_valid else "Not found")},
                    "poNumber": {"isHealthy": True, "summary": "N/A"},
                    "pickUpAddress": {"isHealthy": address_match, "summary": address_notes},
                    "deliveryAddressAddress": {"isHealthy": address_match, "summary": address_notes}
                }
                pod_post_result = await post_paperwork_issue(
                    pod_paperwork_data, file_bytes_value, file_name or "pod_document", file_type=2, group_id=chat_id
                )
                if pod_post_result["success"]:
                    print(f"✅ POD paperwork issue yuborildi! Load #{load_display_id}")
                else:
                    await send_error_to_group(f"❌ POD paperwork issue xatosi: {pod_post_result.get('error')}", group_id=chat_id)

                await msg.answer(f"✅ Load #{load_display_id} POD #{pods_count}/{required_pods} received!")
            else:
                error_msg = result.get('error', 'Unknown error')
                await send_error_to_group(f"❌ POD verification error: {error_msg}", group_id=chat_id)
                await msg.answer(f"❌ POD verification failed for Load #{load_display_id}. Please try again.")
    else:
        bol_data = check_result.get('bol_data', {})
        available_loads = check_result.get('available_loads', 0)
        print(f"📋 else block entered: bol_data={bol_data}, available_loads={available_loads}")

        pickup = bol_data.get('pickup', 'not found')
        delivery = bol_data.get('delivery', 'not found')

        if check_result.get('loadId'):
            load_id = check_result['loadId']
            await msg.answer(f"⚠️ Load #{load_id} BOL verified, but could not save to server.\nPlease try again or contact support.")
            await checking_msg.delete()
            return

        if pickup == 'not found' and delivery == 'not found':
            print(f"📄 Could not extract BOL data from document")
            await checking_msg.delete()
            return

        print(f"📋 Sending 'BOL did not match' message to user")
        await checking_msg.delete()
        await msg.answer("❌ BOL did not match any load.")


@dp.callback_query(F.data == "pending_bol")
async def pending_bol_callback(callback: types.CallbackQuery):
    """Driver BOL tugmasini bosdi — pending PDF ni BOL sifatida tekshir"""
    chat_id = callback.message.chat.id
    group_key = str(chat_id)
    pending = GROUP_PENDING_IMAGES.get(group_key)

    # Timeout taskni bekor qilish — driver o'zi bosdi
    timeout_task = GROUP_IMAGE_TIMEOUT_TASKS.pop(group_key, None)
    if timeout_task and not timeout_task.done():
        timeout_task.cancel()

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)

    if not pending or not pending.get("pages"):
        return

    try:
        pdf_bytes = build_pdf_from_images(pending["pages"])
        file_name = f"doc_{callback.message.message_id}.pdf"
        GROUP_PENDING_IMAGES.pop(group_key, None)
        checking_msg = await callback.message.answer("🔍 Checking document...")
        await run_bol_check(chat_id, pdf_bytes, file_name, callback.message, checking_msg)
    except Exception as e:
        await send_error_to_group(f"❌ pending_bol error: {e}", group_id=chat_id)
        await callback.message.answer(f"❌ Error: {e}")


@dp.callback_query(F.data == "pending_docs")
async def pending_docs_callback(callback: types.CallbackQuery):
    """Driver DOCS tugmasini bosdi — pending PDF ni oddiy hujjat sifatida saqlash"""
    chat_id = callback.message.chat.id
    group_key = str(chat_id)
    GROUP_PENDING_IMAGES.pop(group_key, None)
    timeout_task = GROUP_IMAGE_TIMEOUT_TASKS.pop(group_key, None)
    if timeout_task and not timeout_task.done():
        timeout_task.cancel()
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("📄 Document saved.")


@dp.message(F.document | F.photo)
async def handle_documents(msg: types.Message):
    """BOL va POD hujjatlarini qabul qilish va tekshirish (isComplete-based)"""
    chat_id = msg.chat.id
    user_id = msg.from_user.id

    if chat_id not in STARTED_GROUPS:
        return

    # Check if group is registered (API dan tekshirish - cache emas)
    chat_name = msg.chat.title or msg.from_user.full_name or "Group"
    is_registered = await wait_for_server_and_check(chat_id, chat_name, msg, force_check=True)
    if not is_registered:
        await msg.answer("❌ Group is not registered. Please use /start to register first.")
        return

    # === CAPTION DA CHECKIN/CHECKOUT (HAMMA UCHUN) ===
    if msg.caption:
        caption_text = build_checkin_checkout_text(msg, msg.caption)
        await process_checkin_checkout_text(caption_text, chat_id, msg)

    # Only drivers can send documents for BOL/POD processing - ignore others silently
    print(f"🔍 handle_documents | user_id={user_id} | primary={get_group_driver(chat_id)} | team={get_team_driver(chat_id)}")
    if not is_any_driver(chat_id, user_id):
        return

    # Check bolPodPaperworkAnalysis permission
    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("bolPodPaperworkAnalysis", True):
            return

    # Faylni olish
    is_photo = False
    if msg.photo:
        is_photo = True
        file_id = msg.photo[-1].file_id
        file_name = f"doc_{msg.message_id}.pdf"
    elif msg.document:
        if msg.document.mime_type not in ('application/pdf', 'image/jpeg', 'image/png', 'image/jpg'):
            return
        is_photo = msg.document.mime_type in ('image/jpeg', 'image/png', 'image/jpg')
        file_id = msg.document.file_id
        file_name = msg.document.file_name
    else:
        return

    # Faylni yuklab olish
    try:
        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        file_bytes_value = file_bytes.getvalue()

        # === RASM: pending ga qo'sh, PDF yasab tugma ko'rsat ===
        if is_photo and company_id:
            photo_perms = await get_company_permissions(str(company_id))
            if photo_perms and not photo_perms.get("photoPdf", True):
                return
        if is_photo:
            group_key = str(chat_id)
            now = time.time()
            pending = GROUP_PENDING_IMAGES.get(group_key)

            # 5 daqiqadan ko'p o'tgan bo'lsa — yangi guruh boshlanadi
            if pending and (now - pending.get("last_image_time", now)) > 300:
                GROUP_PENDING_IMAGES.pop(group_key, None)
                old_task = GROUP_IMAGE_TIMEOUT_TASKS.pop(group_key, None)
                if old_task and not old_task.done():
                    old_task.cancel()
                pending = None

            pending = GROUP_PENDING_IMAGES.setdefault(group_key, {"pages": [], "prompt_msg_id": None, "last_image_time": now})
            pending["last_image_time"] = now
            pending["pages"].append(file_bytes_value)

            # Avvalgi debounce taskni bekor qilib yangi task yaratish
            existing_task = GROUP_IMAGE_DEBOUNCE_TASKS.get(group_key)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            GROUP_IMAGE_DEBOUNCE_TASKS[group_key] = asyncio.create_task(
                _send_image_prompt(chat_id, group_key, msg)
            )
            return

        await run_bol_check(chat_id, file_bytes_value, file_name, msg)

    except Exception as e:
        import traceback
        await send_error_to_group(f"❌ Document verification error: {e}", group_id=chat_id)
        print(traceback.format_exc())
        await msg.answer(f"❌ Document verification failed: {str(e)}")


# ======================================================
# === Voice Message Handler (Checkin/Checkout) ===
# ======================================================
@dp.message(F.voice | F.audio)
async def handle_voice(msg: types.Message):
    """Ovozli xabar yoki audio fayldan checkin/checkout parse qilish"""
    chat_id = msg.chat.id
    user_id = msg.from_user.id

    if chat_id not in STARTED_GROUPS:
        return

    chat_name = msg.chat.title or msg.from_user.full_name or "Group"
    is_registered = await check_group_registered_force(chat_id, chat_name, force_check=False)
    if not is_registered:
        return

    if not is_any_driver(chat_id, user_id):
        return

    try:
        if msg.voice:
            file_id = msg.voice.file_id
        else:
            file_id = msg.audio.file_id

        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        file_bytes_value = file_bytes.getvalue()

        audio_file = BytesIO(file_bytes_value)
        audio_file.name = "voice.ogg"

        loop = asyncio.get_event_loop()
        transcript = await loop.run_in_executor(
            None,
            lambda: client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        )
        text = transcript.text.strip()
        print(f"🎤 Voice transcribed: {text}")

        if not text:
            return

        voice_checkin_text = build_checkin_checkout_text(msg, text)
        await process_checkin_checkout_text(voice_checkin_text, chat_id, msg)

    except Exception as e:
        import traceback
        await send_error_to_group(f"❌ Voice handler error: {e}", group_id=chat_id)
        print(traceback.format_exc())


# ======================================================
# === Run Telegram Bot ===
# ======================================================
async def run_bot():
    print("🤖 Telegram bot is starting...")
    await get_api_token()
    await dp.start_polling(bot)


# ======================================================
# === Main entry point ===
# ======================================================
async def main():
    print("🚀 Starting combined Telegram bot and FastAPI server...")
    print("📊 Available endpoints:")
    print("   - Telegram bot: Listening for messages")
    print("   - BOT API: http://localhost:8045/docs")
    print("   - BOL Check: POST http://localhost:8045/check-bol")

    await init_db()

    # Restart da /start bosilgan guruhlarni qayta yuklash
    STARTED_GROUPS.update(load_started_groups())
    if STARTED_GROUPS:
        print(f"♻️ Restored {len(STARTED_GROUPS)} started groups")

    # DB dan driverlarni yuklash (restart da ham saqlanadi)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT group_id, driver_id, driver_name, team_driver_id, team_driver_name FROM groups WHERE driver_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            for group_id, driver_id, driver_name, team_driver_id, team_driver_name in rows:
                GROUP_DRIVER_IDS[group_id] = driver_id
                print(f"♻️ Restored driver {driver_name} (ID: {driver_id}) for group {group_id}")
                if team_driver_id:
                    GROUP_TEAM_DRIVERS[group_id] = team_driver_id
                    print(f"♻️ Restored team driver {team_driver_name} (ID: {team_driver_id}) for group {group_id}")

    # Restart bo'lganda JSON'dan "todo" statuslarni memory'ga yuklash
    all_data = load_all_group_tokens()
    for group_id, group_data in all_data.items():
        saved_status = group_data.get("ticket_status", "")
        if saved_status == "todo":
            updated_at_str = group_data.get("ticket_status_updated_at")
            GROUP_TICKET_STATUS[group_id] = {
                "status": "todo",
                "created_at": updated_at_str or datetime.now().isoformat(),
                "done_at": None
            }
            polling_task = asyncio.create_task(poll_backend_ticket_status(group_id))
            GROUP_TICKET_POLLING_TASKS[group_id] = polling_task
            print(f"♻️ Restored ticket status 'todo' for group {group_id}")

    worker_task = asyncio.create_task(message_worker())
    print("📨 Message worker started")

    retry_task = asyncio.create_task(retry_failed_messages())
    print("🔁 Failed messages retry worker started")

    # Uvicorn config
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=int(os.environ.get("BOT_PORT", "8045")),
        log_level="info"
    )
    server = uvicorn.Server(config)
    bot_task = asyncio.create_task(run_bot())

    await asyncio.gather(server.serve(), bot_task, worker_task, retry_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"❌ Server error: {e}")
