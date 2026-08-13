"""Chat-log ma'lumotlarini o'qish — `api/chatlog.py` (admin UI) va `api/ai_chat.py`
(tashqi AI agent API) ikkalasi ham shu yerdan foydalanadi, faqat auth qatlami har xil.

Yozuv tomoni: `telegram/chat_logger.py` (outer middleware).
"""
import logging
import mimetypes
from datetime import UTC, datetime

from config import DB_PATH
from db.connect import db_connect
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from groups import load_all_group_tokens
from state import bot

logger = logging.getLogger(__name__)

_FILE_CONTENT_TYPE = {"photo": "image/jpeg", "voice": "audio/ogg", "video": "video/mp4"}


async def fetch_groups(company_id: str | None = None) -> list[dict]:
    async with db_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT g.group_id, g.title, g.updated_at, "
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.group_id = g.group_id) AS msg_count, "
            "(SELECT text FROM chat_messages m WHERE m.group_id = g.group_id ORDER BY m.id DESC LIMIT 1) AS last_text, "
            "(SELECT msg_type FROM chat_messages m WHERE m.group_id = g.group_id ORDER BY m.id DESC LIMIT 1) AS last_type "
            "FROM chat_groups g ORDER BY g.updated_at DESC"
        ) as cur:
            rows = await cur.fetchall()

    tokens = load_all_group_tokens()  # RAM cache — companyId shu yerdan (chat_groups'da yo'q)
    items = [
        {
            "group_id": r[0], "title": r[1], "updated_at": r[2],
            "msg_count": r[3], "last_text": r[4], "last_type": r[5],
            "company_id": (tokens.get(str(r[0])) or {}).get("companyId"),
        }
        for r in rows
    ]
    if company_id is not None:
        # Solishtirish STRING'da — cache'da int ham, str ham uchraydi.
        items = [g for g in items if str(g["company_id"]) == str(company_id)]
    return items


async def fetch_messages(group_id: str, before_id: int = 0, limit: int = 50) -> tuple[list[dict], bool]:
    limit = max(1, min(int(limit or 50), 200))

    sql = (
        "SELECT id, message_id, user_id, user_name, msg_type, text, reply_to_message_id, created_at, "
        "reply_user_name, reply_text, reply_msg_type, file_id, file_name, file_size "
        "FROM chat_messages WHERE group_id = ?"
    )
    params: list = [group_id]
    if before_id:
        sql += " AND id < ?"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    async with db_connect(DB_PATH) as db:
        async with db.execute(sql, tuple(params)) as cur:
            rows = list(await cur.fetchall())  # Iterable[Row] → list (reversed/len uchun)

    items = [
        {
            "id": r[0], "message_id": r[1], "user_id": r[2], "user_name": r[3],
            "msg_type": r[4], "text": r[5], "reply_to_message_id": r[6], "created_at": r[7],
            "reply_user_name": r[8], "reply_text": r[9], "reply_msg_type": r[10],
            "has_file": r[11] is not None, "file_name": r[12], "file_size": r[13],
        }
        for r in reversed(rows)  # eski -> yangi, o'qish tartibida
    ]
    return items, len(rows) == limit


async def fetch_group_title(group_id: str) -> str | None:
    """Bitta guruh nomi (`fetch_groups` og'ir COUNT'larisiz)."""
    async with db_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT title FROM chat_groups WHERE group_id = ?", (group_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def fetch_messages_window(
    group_id: str,
    since=None,
    until=None,
    max_messages: int = 1000,
) -> list[dict]:
    """Vaqt oynasi bo'yicha xabarlar, eskidan-yangiga (AI tahlili uchun).

    `created_at` PG'da TIMESTAMPTZ (datetime), SQLite'da TEXT — shuning uchun
    filtr SQL'da EMAS, Python'da (`as_dt` normalizatsiyasi bilan) qilinadi:
    ikki backend'ning timestamp semantikasi bo'yicha jimgina farq qilish riski yo'q.

    O'qish `(group_id, id)` indeksidagi keyset naqshi bilan, sahifama-sahifa
    orqaga — oyna oxirgi N xabardan eskiroq bo'lsa ham to'g'ri topiladi.
    """
    max_messages = max(1, min(int(max_messages or 1000), 5000))
    since_dt, until_dt = as_dt(since), as_dt(until)

    def _in_window(row: dict) -> bool:
        ts = as_dt(row["created_at"])
        if ts is None:
            return not (since_dt or until_dt)
        if since_dt and ts < since_dt:
            return False
        return not (until_dt and ts > until_dt)

    collected: list[dict] = []
    before_id = 0
    # Sahifa-skani chegarasi: `until` uzoq o'tmishni ko'rsatsa oynagacha yetib borish
    # kerak, lekin cheksiz emas — 50 × 200 = 10k qator eng ko'pi.
    for _ in range(50):
        page, has_more = await fetch_messages(group_id, before_id, 200)
        if not page:
            break
        # `fetch_messages` eskidan-yangiga qaytaradi; eng eskisi — page[0].
        before_id = page[0]["id"]
        # Oynadan tashqaridagi qatorlar limitni yemasin (aks holda eski oyna
        # so'ralganda bo'sh natija qaytardi).
        collected = [m for m in page if _in_window(m)] + collected
        oldest = as_dt(page[0]["created_at"])
        if since_dt and oldest and oldest < since_dt:
            break  # oynadan o'tib ketdik — orqaga yurishning hojati yo'q
        if len(collected) >= max_messages or not has_more:
            break

    return collected[-max_messages:]


def as_dt(value):
    """`created_at` (datetime | ISO str | 'YYYY-MM-DD HH:MM:SS') → tz-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("chat_data.as_dt: tushunarsiz sana '%s'", text[:40])
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def proxy_file(msg_pk_id: int) -> StreamingResponse:
    """Rasm/fayl/ovoz/video — Telegram'dan bot tokeni bilan real vaqtda proksi qilinadi
    (blob bazaga yozilmagan, faqat file_id saqlangan)."""
    async with db_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT msg_type, file_id, file_name FROM chat_messages WHERE id = ?", (msg_pk_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[1]:
        raise HTTPException(status_code=404, detail="Fayl topilmadi")
    msg_type, file_id, file_name = row

    try:
        tg_file = await bot.get_file(file_id)
        # file_path/None va bo'sh buf holatlari except'ga tushadi → 502 (xulq o'zgarmagan)
        buf = await bot.download_file(tg_file.file_path or "")
        if buf is None:
            raise RuntimeError("bo'sh javob (download_file None)")
    except Exception:
        logger.warning("chat_data.proxy_file: Telegram'dan yuklab bo'lmadi (file_id=%s)", file_id, exc_info=True)
        raise HTTPException(status_code=502, detail="Telegram'dan fayl olinmadi") from None

    content_type = (
        (mimetypes.guess_type(file_name)[0] if file_name else None)
        or _FILE_CONTENT_TYPE.get(msg_type)
        or "application/octet-stream"
    )
    disposition = "attachment" if msg_type == "document" else "inline"
    headers = {"Cache-Control": "private, max-age=86400"}
    if file_name:
        headers["Content-Disposition"] = f'{disposition}; filename="{file_name}"'
    return StreamingResponse(buf, media_type=content_type, headers=headers)
