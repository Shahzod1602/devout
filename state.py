"""Runtime state singleton: mutable in-memory dicts + long-lived client objects.

Bu modul boshqa bot modullariga BOG'LIQ EMAS — barcha modullar shu yerdan import
qiladi, shuning uchun circular import yo'q. `bot.py` shim ham re-export qiladi
(legacy `from bot import REGISTERED_GROUPS` ishlatuvchilar uchun).

Qoidalar:
- Bu yerdagi dict/set/list'larni QAYTA BIND qilmang — faqat mutate qiling
  (`.update()`, `[key] = val`, `.add()`, `.pop()`). Aks holda re-bind import
  qiluvchi modul ko'rmaydi.
- Bu yerga FUNKSIYA QO'YMANG — funksiyalar dedicated modullarda (groups,
  tickets, messaging, va h.k.).
"""
import asyncio

from aiogram import Bot, Dispatcher
from config import BOT_TOKEN, CEREBRAS_API_KEY, ERROR_BOT_TOKEN, GROQ_API_KEY, OPENAI_API_KEY
from openai import OpenAI

# === Telegram singleton clients ===
bot: Bot = Bot(token=BOT_TOKEN)
dp: Dispatcher = Dispatcher()
error_bot: Bot = Bot(token=ERROR_BOT_TOKEN)

# === LLM clients ===
openai_client: OpenAI = OpenAI(api_key=OPENAI_API_KEY)
groq_client: OpenAI = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
# Cerebras — Groq ishlamay qolsa fallback (checkin.py ishlatadi). Key bo'lmasa None.
cerebras_client: OpenAI | None = (
    OpenAI(api_key=CEREBRAS_API_KEY, base_url="https://api.cerebras.ai/v1")
    if CEREBRAS_API_KEY else None
)

# Backward-compat alias — bot.py historically exposed `client` for OpenAI.
client: OpenAI = openai_client

# === Queues ===
message_queue: asyncio.Queue = asyncio.Queue()

# === Group/driver registration state ===
GROUP_DRIVER_IDS: dict[str, int] = {}        # group_id (str) -> primary driver_id (int)
GROUP_TEAM_DRIVERS: dict[str, int] = {}      # group_id (str) -> team driver_id (int)
AWAITING_TOKEN: dict = {}                    # group_id -> user_id who must reply with token
TOKEN_FAILED_ATTEMPTS: dict = {}             # group_id -> failed validation count
REGISTERED_GROUPS: dict = {}                 # group_id (both int and str keys) -> True
STARTED_GROUPS: set = set()                  # /start muvaffaqiyatli ishlagan guruh ID'lari

# === Image batching (paperwork) ===
GROUP_PENDING_IMAGES: dict = {}              # group_id (str) -> {"pages": [...], "prompt_msg_id": int|None, "last_image_time": float}
GROUP_IMAGE_DEBOUNCE_TASKS: dict = {}        # group_id (str) -> asyncio.Task
GROUP_IMAGE_TIMEOUT_TASKS: dict = {}         # group_id (str) -> asyncio.Task

# Paperwork-notify "Open original" link correlation. Backend internal-team/driver
# notify xabarini render qilib /send-message orqali yuboradi — payload'da asl
# hujjat linki bo'lmaydi. Bot hujjatni ishlaganda RefNumber (load_display_id) ->
# asl xabar linkini shu yerga yozadi; message_worker notify matniga qo'shadi.
PAPERWORK_MSG_LINKS: dict = {}               # ref_number (str) -> "https://t.me/c/<internal>/<msg_id>"

# === Cooldown / conversation tracking ===
DRIVER_COOLDOWN: dict = {}                   # driver_id -> last_ticket_time
CONVERSATION_LAST_TIME: dict = {}            # group_id -> last_conversation_time
LAST_TICKETS: dict = {}                      # group_id -> last_ticket_text

# === Ticket lifecycle ===
ACCEPTED_STATUS: dict = {}                   # group_id -> True/False
GROUP_TICKET_STATUS: dict = {}               # group_id -> {"status": "todo"|"done", "created_at": ts, "done_at": ts}
GROUP_TICKET_TIMERS: dict = {}               # group_id -> timer_task for status reset
GROUP_TICKET_MESSAGES: dict = {}             # group_id -> [{"writerName", "message", "timestamp"}, ...]
GROUP_TICKET_POLLING_TASKS: dict = {}        # group_id -> polling_task for backend status check

# === Retry queues ===
FAILED_MESSAGES_QUEUE: list = []             # [{"groupId", "writerName", "message"}, ...]
HISTORY_SENT_MESSAGE_KEYS: set = set()       # {"<chat_id>:<message_id>", ...}
