"""Chat log — group orqali o'tgan HAR BIR xabarni yozadi + live UI'ga push qiladi.

`ChatLogMiddleware` — aiogram outer middleware (dp.message.outer_middleware).
Outer middleware har bir Message update uchun ISHLAYDI, filter/handler'lardan
OLDIN — shuning uchun mavjud handler'lardagi (generic_text_handler va h.k.)
early-return'lar (unregistered group, internal team, wrong sender) bu yerga
ta'sir qilmaydi: STARTED_GROUPS holatidan qat'iy nazar hammasi yoziladi.

Xatolik hech qachon bot pipeline'ini buzmasligi shart — barcha DB/broadcast
ishlari try/except ichida, handler chain doim davom etadi.
"""
import logging
from asyncio import Queue

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from config import CHAT_LOG_ENABLE, DB_PATH
from db.connect import db_connect

logger = logging.getLogger(__name__)

# group_id (str) -> subscriber queue'lar to'plami. Bitta process ichida FastAPI
# app va bot bir xil event loop'da ishlaydi — tashqi broker kerak emas.
_SUBSCRIBERS: dict[str, set[Queue]] = {}


def subscribe(group_id: str) -> Queue:
    q: Queue = Queue(maxsize=200)
    _SUBSCRIBERS.setdefault(group_id, set()).add(q)
    return q


def unsubscribe(group_id: str, q: Queue) -> None:
    subs = _SUBSCRIBERS.get(group_id)
    if subs is not None:
        subs.discard(q)
        if not subs:
            _SUBSCRIBERS.pop(group_id, None)


def _publish(group_id: str, payload: dict) -> None:
    for q in list(_SUBSCRIBERS.get(group_id, ())):
        if q.full():
            continue  # sekin/tashlab ketilgan client — yangi xabarlarni yo'qotamiz, crash emas
        q.put_nowait(payload)


def _msg_type(msg: Message) -> str:
    if msg.text is not None:
        return "text"
    if msg.photo:
        return "photo"
    if msg.document:
        return "document"
    if msg.voice or msg.audio:
        return "voice"
    if msg.video:
        return "video"
    if msg.sticker:
        return "sticker"
    return "other"


def _file_meta(msg: Message) -> tuple[str | None, str | None, int | None]:
    """(file_id, file_name, file_size) — /admin/chat/api/file/{id} shu file_id bilan
    Telegram'dan real vaqtda yuklab beradi (blob bazaga yozilmaydi)."""
    if msg.photo:
        largest = msg.photo[-1]
        return largest.file_id, None, largest.file_size
    if msg.document:
        return msg.document.file_id, msg.document.file_name, msg.document.file_size
    if msg.voice:
        return msg.voice.file_id, None, msg.voice.file_size
    if msg.audio:
        return msg.audio.file_id, msg.audio.file_name, msg.audio.file_size
    if msg.video:
        return msg.video.file_id, msg.video.file_name, msg.video.file_size
    return None, None, None


class ChatLogMiddleware(BaseMiddleware):
    # event: TelegramObject — BaseMiddleware imzosi (LSP); dp.message.outer_middleware'da
    # amalda doim Message keladi, isinstance shu invariantni tipga tushiradi.
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not CHAT_LOG_ENABLE:
            return await handler(event, data)

        if isinstance(event, Message):
            try:
                await self._log(event)
            except Exception:  # noqa: BLE001 — logging hech qachon botni yiqitmasin
                logger.warning("chat_logger: failed to log message", exc_info=True)

        return await handler(event, data)

    async def _log(self, msg: Message) -> None:
        if msg.chat.type not in ("group", "supergroup"):
            return

        group_id = str(msg.chat.id)
        title = msg.chat.title or group_id
        user_id = msg.from_user.id if msg.from_user else None
        user_name = msg.from_user.full_name if msg.from_user else None
        msg_type = _msg_type(msg)
        text = msg.text if msg.text is not None else msg.caption

        replied = msg.reply_to_message
        reply_to = replied.message_id if replied else None
        # Telegram javob berilgan xabarni TO'LIQ beradi (replied) — DB'dan qidirish
        # shart emas, hali loglanmagan/eski xabarga javob bo'lsa ham ishlaydi.
        reply_user_name = (replied.from_user.full_name if replied and replied.from_user else None)
        reply_text = (replied.text if replied and replied.text is not None else (replied.caption if replied else None))
        reply_msg_type = _msg_type(replied) if replied else None

        file_id, file_name, file_size = _file_meta(msg)

        async with db_connect(DB_PATH) as db:
            async with db.execute(
                "INSERT INTO chat_messages "
                "(group_id, message_id, user_id, user_name, msg_type, text, reply_to_message_id, "
                "reply_user_name, reply_text, reply_msg_type, file_id, file_name, file_size) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (group_id, msg.message_id, user_id, user_name, msg_type, text, reply_to,
                 reply_user_name, reply_text, reply_msg_type, file_id, file_name, file_size),
            ) as cur:
                row = await cur.fetchone()
                pk_id = row[0] if row else None
            await db.execute(
                "INSERT INTO chat_groups (group_id, title, updated_at) VALUES (?, ?, datetime('now')) "
                "ON CONFLICT (group_id) DO UPDATE SET title=excluded.title, updated_at=datetime('now')",
                (group_id, title),
            )
            await db.commit()

        _publish(group_id, {
            "id": pk_id,
            "message_id": msg.message_id,
            "user_id": user_id,
            "user_name": user_name,
            "msg_type": msg_type,
            "text": text,
            "reply_to_message_id": reply_to,
            "reply_user_name": reply_user_name,
            "reply_text": reply_text,
            "reply_msg_type": reply_msg_type,
            "file_name": file_name,
            "file_size": file_size,
            "has_file": file_id is not None,
            "created_at": msg.date.isoformat() if msg.date else None,
        })
