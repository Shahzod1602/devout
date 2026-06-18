"""Telegram error logging, backend action log, va queue worker.

- send_error_to_group: ERROR_GROUP_ID'ga (error logger bot orqali) xato yuborish
- send_action_log: backend'ga audit log POST qilish
- _linkify_maps: Google Maps URL'larni HTML link'ga aylantirish (worker uchun)
- message_worker: message_queue'dan xabarlarni o'qib Telegram'ga yuborish
"""
import asyncio
import logging
import re
from datetime import datetime

import aiohttp
import requests
from config import ACTION_LOGS_URL, BOT_TOKEN, ENV_LABEL, ERROR_GROUP_ID, PAPERWORK_LOG_GROUP_ID, ssl_context
from external import get_api_token
from state import bot, error_bot, message_queue

logger = logging.getLogger(__name__)


async def send_error_to_group(message: str, group_id=None):
    """ERROR_GROUP_ID'ga xato xabari yuborish. Guruh nomi mavjud bo'lsa, label sifatida qo'shadi."""
    if not ERROR_GROUP_ID:
        return
    # Late import: `groups` ham bu modulni import qiladi (circular dependency'ni
    # sindirish uchun). Error path'da kechikishning ahamiyati yo'q.
    from groups import load_all_group_tokens
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
        await error_bot.send_message(
            ERROR_GROUP_ID,
            f"❌ <b>[{ENV_LABEL}]</b>{group_label} {message}\n🕐 {now}",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("send_error_to_group: error_bot.send_message failed")


async def send_paperwork_to_log_group(file_bytes: bytes, file_name: str, status: str,
                                      reason: str, chat_id=None, load_id=None):
    """Har bir paperwork faylini PAPERWORK_LOG_GROUP_ID guruhiga forward qilish.

    status: "selected" (tahlil qilindi) yoki "skipped" (o'tkazib yuborildi).
    Caption'da: status, sabab, manba guruh nomi va Load # (bo'lsa).
    PAPERWORK_LOG_GROUP_ID=0 bo'lsa hech narsa qilmaydi (feature o'chiq).
    """
    if not PAPERWORK_LOG_GROUP_ID:
        return
    from aiogram.types import BufferedInputFile

    # Late import: groups ham bu modulni import qiladi (circular dependency).
    from groups import load_all_group_tokens
    try:
        group_label = ""
        if chat_id is not None:
            gid_str = str(chat_id)
            data = load_all_group_tokens()
            group_name = data.get(gid_str, {}).get("group_name", "")
            group_label = f" • {group_name}" if group_name else f" • group:{gid_str}"

        is_selected = status == "selected"
        icon = "✅" if is_selected else "⏭️"
        head = "SELECTED" if is_selected else "SKIPPED"
        load_part = f" — Load #{load_id}" if load_id not in (None, "", "N/A") else ""
        caption = f"{icon} {head}{load_part}\n📝 {reason}\n📦 [{ENV_LABEL}]{group_label}"

        await bot.send_document(
            PAPERWORK_LOG_GROUP_ID,
            BufferedInputFile(file_bytes, filename=file_name or "document.pdf"),
            caption=caption[:1024],
        )
    except Exception:
        logger.exception("send_paperwork_to_log_group: failed to forward file")


async def send_action_log(group_id, message):
    """Backend audit log API'ga POST. Birinchi so'zni <mark> bilan o'rab beradi."""
    token = await get_api_token()
    if not token:
        return False

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
        "groupId": str(group_id),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "EN",
        "Content-Type": "application/json",
        "X-Group-Id": str(group_id),
    }

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(ACTION_LOGS_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                response_text = await resp.text()
                if resp.status in (200, 201):
                    logger.info("✅ Action log sent for group %s", group_id)
                    return True
                else:
                    logger.warning("⚠️ Action log error [%s]: %s", resp.status, response_text)
                    return False
    except Exception:
        logger.exception("❌ Error sending action log")
        return False


# ====== Queue worker ======

def _linkify_maps(text: str) -> str:
    """Google Maps URL'larini HTML anchor'larga aylantirish.

    - 'Location: <manzil>\\n📍 URL' → 'Location: <a href="URL">manzil</a>'
    - Boshqa URL'lar → '<a href="URL">Click here to view on map</a>'
    """
    def replace_with_address(m):
        prefix = m.group(1)
        address = m.group(2)
        url = m.group(3)
        return f'{prefix}<a href="{url}">{address}</a>'

    text = re.sub(
        r'((?:Current location|Location):\s*)([^\n]+)\n📍\s*(https://www\.google\.com/maps\?q=[\d.,-]+)',
        replace_with_address,
        text,
    )
    text = re.sub(
        r'📍\s*(https://www\.google\.com/maps\?q=[\d.,-]+)',
        lambda m: f'<a href="{m.group(1)}">Click here to view on map</a>',
        text,
    )
    text = re.sub(
        r'(?<!["\'])https://www\.google\.com/maps\?q=[\d.,-]+',
        lambda m: f'<a href="{m.group(0)}">Click here to view on map</a>',
        text,
    )
    return text


async def message_worker():
    """FastAPI /send-message endpoint'i tomonidan to'ldirilgan message_queue iste'molchisi.

    Har bir xabarni Telegram Bot API orqali yuboradi, `has_pin_required` bo'lsa pin qiladi.
    Xatolarni ERROR_GROUP_ID'ga yuboradi.
    """
    TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
    while True:
        try:
            data = await message_queue.get()
            message_text = _linkify_maps(data.message)
            send_payload = {"chat_id": data.group_id, "text": message_text, "parse_mode": "HTML"}
            if data.inline_buttons:
                # Pydantic InlineButton'larni Telegram Bot API ko'rinishiga aylantirish.
                # `None` field'lar tashlanadi — Telegram bir button uchun faqat bitta
                # action turini (callback_data yoki url) qabul qiladi.
                send_payload["reply_markup"] = {
                    "inline_keyboard": [
                        [
                            {
                                k: v
                                for k, v in {"text": b.text, "callback_data": b.callback_data, "url": b.url}.items()
                                if v is not None
                            }
                            for b in row
                        ]
                        for row in data.inline_buttons
                    ]
                }
            try:
                # NOTE: sync requests in async — FAZA 7'da httpx.AsyncClient ga ko'chiriladi.
                send_response = requests.post(f"{TELEGRAM_API_BASE}/sendMessage", json=send_payload)  # noqa: ASYNC210
                send_result = send_response.json()
                if send_result.get("ok"):
                    message_id = send_result["result"]["message_id"]
                    logger.info("📨 Message sent to group %s: %s...", data.group_id, data.message[:50])
                    if data.has_pin_required:
                        pin_payload = {"chat_id": data.group_id, "message_id": message_id,
                                       "disable_notification": False}
                        pin_response = requests.post(f"{TELEGRAM_API_BASE}/pinChatMessage", json=pin_payload)  # noqa: ASYNC210
                        pin_result = pin_response.json()
                        if pin_result.get("ok"):
                            logger.info("📌 Message pinned in group %s", data.group_id)
                else:
                    await send_error_to_group(f"❌ Error sending message: {send_result.get('description', 'Unknown error')}", group_id=data.group_id)
            except Exception as e:
                await send_error_to_group(f"❌ Error sending message: {e}", group_id=data.group_id)
            message_queue.task_done()
        except Exception as e:
            await send_error_to_group(f"❌ Worker error: {e}")
            await asyncio.sleep(1)
