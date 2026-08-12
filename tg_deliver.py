"""Uzun matnni Telegram chatga yetkazish — 4096 bo'lish + flood-control.

Nega yangi modul: `messaging.py` bot/ va botprod/ o'rtasida ATAYLAB farq qiladi
(botprod oldinda) — unga tegish mirror minasi. Bu yerdagi helper ikkala muhitda
bayt-ma-bayt bir xil qoladi.

Nega `message_queue` emas: navbat `/send-message` webhook'i uchun (backend →
guruh, pin/tugma mantig'i bilan). Bu yerda oddiy matn va chaqiruvchi natijani
BILISHI kerak (yetkazildimi, nechta qismda) — shuning uchun to'g'ridan-to'g'ri.

Matn **formatlanmagan** (parse_mode YO'Q): mazmun LLM'dan keladi, uning
`**bold**`/`_` belgilariga Telegram parseri tez-tez "can't parse entities" bilan
yiqiladi va butun xabar yo'qoladi. Xom matn har doim yetib boradi.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from state import bot

logger = logging.getLogger(__name__)

# Telegram chegarasi 4096; zaxira qoldiramiz (emoji/surrogate hisobi qat'iy emas).
_PART_LIMIT = 3900
_MAX_PARTS = 20                 # ~78k belgi — undan ortig'i chat uchun ma'nosiz
_RETRY_AFTER_CAP_S = 60.0       # flood-control juda uzun kutish so'rasa — voz kechamiz


def split_for_telegram(text: str, limit: int = _PART_LIMIT) -> list[str]:
    """Matnni Telegram limitiga bo'lish — imkon qadar QATOR chegarasidan.

    Juda uzun bitta qator (masalan bo'linmagan URL yoki transkript satri) qattiq
    kesiladi — aks holda umuman yuborib bo'lmaydi.
    """
    if not text:
        return []
    parts: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:  # bir o'zi sig'maydigan qator
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{buf}\n{line}" if buf else line
        if len(candidate) > limit:
            parts.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts


async def deliver_text(chat_id: str | int, text: str) -> dict:
    """Matnni chatga yuboradi (kerak bo'lsa bir necha qismda). Hech qachon raise qilmaydi.

    Qaytaradi: `{"ok": bool, "parts": int, "sent": int, "error": str | None}`.
    """
    parts = split_for_telegram(text, _PART_LIMIT)
    if not parts:
        return {"ok": False, "parts": 0, "sent": 0, "error": "bo'sh matn"}

    truncated = len(parts) > _MAX_PARTS
    if truncated:
        parts = parts[:_MAX_PARTS]
        parts[-1] += "\n\n… [hisobot juda uzun — qolgani kesildi]"

    sent = 0
    for idx, part in enumerate(parts, start=1):
        prefix = f"({idx}/{len(parts)})\n" if len(parts) > 1 else ""
        try:
            await _send_with_retry(chat_id, prefix + part)
            sent += 1
        except TelegramAPIError as e:
            # 403 = CEO botga /start bosmagan (bot suhbatni o'zi boshlay olmaydi),
            # 400 = chat topilmadi. Ikkalasi ham qayta urinishga arzimaydi.
            logger.warning("tg_deliver: chat=%s qismi %d/%d yuborilmadi: %s",
                           chat_id, idx, len(parts), e)
            return {"ok": False, "parts": len(parts), "sent": sent, "error": str(e)}
    return {"ok": True, "parts": len(parts), "sent": sent, "error": None}


async def _send_with_retry(chat_id: str | int, text: str) -> None:
    """Bitta xabar; flood-control (429 RetryAfter) da bir marta kutib qayta urinadi."""
    try:
        await bot.send_message(chat_id, text, disable_web_page_preview=True)
    except TelegramRetryAfter as e:
        wait = min(float(e.retry_after) + 0.5, _RETRY_AFTER_CAP_S)
        logger.warning("tg_deliver: flood-control, %.1fs kutamiz (chat=%s)", wait, chat_id)
        await asyncio.sleep(wait)
        await bot.send_message(chat_id, text, disable_web_page_preview=True)
