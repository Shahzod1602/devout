"""Telegram error logging, backend action log, va queue worker.

- send_error_to_group: ERROR_GROUP_ID'ga (error logger bot orqali) xato yuborish
- send_action_log: backend'ga audit log POST qilish
- _linkify_maps: Google Maps URL'larni HTML link'ga aylantirish (worker uchun)
- message_worker: message_queue'dan xabarlarni o'qib Telegram'ga yuborish
"""
import asyncio
import html
import logging
import re
from datetime import datetime

import aiohttp
from aiogram.exceptions import TelegramMigrateToChat
from config import ACTION_LOGS_URL, BOT_TOKEN, ENV_LABEL, ERROR_GROUP_ID, PAPERWORK_LOG_GROUP_ID, ssl_context
from external import get_api_token
from state import PAPERWORK_MSG_LINKS, bot, error_bot, message_queue

logger = logging.getLogger(__name__)

# Log-guruh oddiy guruhdan supergroup'ga ko'tarilsa Telegram chat_id'ni o'zgartiradi
# (TelegramMigrateToChat, 2026-07-07 prod'da kuzatildi: -5535325878 → -1004283245217).
# Yangi ID'ni runtime'da eslab qolamiz — restartgacha ham fayllar yangi guruhga boradi.
_LOG_GROUP_MIGRATED_ID: int | None = None

# Eng ko'pi bilan shuncha RefNumber->link yozuvini saqlaymiz (xotira o'smasligi uchun).
PAPERWORK_LINK_CAP = 2000


def build_message_link(chat_id, message_id, username: str | None = None) -> str:
    """Telegram xabariga to'g'ridan-to'g'ri link quradi.

    - Public guruh (username bor) → https://t.me/<username>/<message_id>
    - Private supergroup (id "-100" bilan boshlanadi) → https://t.me/c/<internal>/<message_id>
    - Oddiy guruh (id "-100" emas) yoki message_id yo'q → "" (link mavjud emas).
    """
    if not message_id or chat_id is None:
        return ""
    if username:
        return f"https://t.me/{username}/{message_id}"
    gid_str = str(chat_id)
    if gid_str.startswith("-100"):
        return f"https://t.me/c/{gid_str[4:]}/{message_id}"
    return ""


def remember_paperwork_msg_link(ref, chat_id, message_id) -> None:
    """RefNumber (load_display_id) bo'yicha asl hujjat xabari linkini cache qiladi.

    Backend paperwork-notify xabarini "POD #<RefNumber>" deb render qilib yuboradi,
    lekin payload'da asl link bo'lmaydi — message_worker shu cache'dan topib qo'shadi.
    """
    if ref in (None, "", "N/A"):
        return
    link = build_message_link(chat_id, message_id)
    if not link:
        return
    PAPERWORK_MSG_LINKS[str(ref)] = link
    if len(PAPERWORK_MSG_LINKS) > PAPERWORK_LINK_CAP:
        # Insertion-order: eng eski yozuvni chiqarib tashlaymiz.
        PAPERWORK_MSG_LINKS.pop(next(iter(PAPERWORK_MSG_LINKS)), None)


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
                                      reason: str, chat_id=None, load_id=None, message_id=None):
    """Har bir paperwork faylini PAPERWORK_LOG_GROUP_ID guruhiga forward qilish.

    status: "selected" (tahlil qilindi) yoki "skipped" (o'tkazib yuborildi).
    Caption'da: status, sabab, manba guruh nomi, Load # (bo'lsa) va asl xabarga
    "Open original" message-link (message_id berilgan bo'lsa).
    PAPERWORK_LOG_GROUP_ID=0 bo'lsa hech narsa qilmaydi (feature o'chiq).
    """
    global _LOG_GROUP_MIGRATED_ID
    if not PAPERWORK_LOG_GROUP_ID:
        return
    from aiogram.types import BufferedInputFile

    # Late import: groups ham bu modulni import qiladi (circular dependency).
    from groups import load_all_group_tokens
    try:
        group_label = ""
        msg_link = ""
        if chat_id is not None:
            gid_str = str(chat_id)
            group_name = ""
            username = None
            # Joriy guruh nomini avval Telegram'dan olamiz (eng ishonchli).
            try:
                chat = await bot.get_chat(chat_id)
                group_name = chat.title or chat.full_name or ""
                username = chat.username
            except Exception:
                logger.debug("send_paperwork_to_log_group: get_chat failed for %s", gid_str, exc_info=True)
            # Bo'lmasa — keshlangan token ma'lumotidan.
            if not group_name:
                data = load_all_group_tokens()
                group_name = data.get(gid_str, {}).get("group_name", "")
            group_label = f" • {group_name}" if group_name else f" • group:{gid_str}"

            # Asl xabarga to'g'ridan-to'g'ri link (private supergroup yoki public username).
            if message_id:
                msg_link = build_message_link(chat_id, message_id, username)

        is_selected = status == "selected"
        icon = "✅" if is_selected else "⏭️"
        head = "SELECTED" if is_selected else "SKIPPED"
        load_part = f" — Load #{html.escape(str(load_id))}" if load_id not in (None, "", "N/A") else ""
        # parse_mode=HTML — dinamik qismlarni (sabab, guruh nomi) escape qilamiz va
        # so'ng link anchor'ni qo'shamiz. Caption'ni xom HTML holida kesib qo'ymaymiz
        # (tag o'rtasidan kesilmasligi uchun sababni oldindan qisqartiramiz).
        safe_reason = html.escape((reason or "")[:700])
        safe_group_label = html.escape(group_label)
        caption = f"{icon} {head}{load_part}\n📝 {safe_reason}\n📦 [{ENV_LABEL}]{safe_group_label}"
        if msg_link:
            caption += f'\n🔗 <a href="{msg_link}">Open original</a>'
        # DRIFT-4: Telegram caption limiti 1024 (HTML-escape matnni kengaytirishi mumkin).
        # Oshib ketsa qator chegarasidan kesamiz — HTML tag/entity o'rtasidan kesilmasin.
        if len(caption) > 1024:
            caption = caption[:1000].rsplit("\n", 1)[0] + "\n…"

        target_group = _LOG_GROUP_MIGRATED_ID or PAPERWORK_LOG_GROUP_ID
        try:
            await bot.send_document(
                target_group,
                BufferedInputFile(file_bytes, filename=file_name or "document.pdf"),
                caption=caption,
                parse_mode="HTML",
            )
        except TelegramMigrateToChat as e:
            # Guruh supergroup bo'lgan — yangi ID bilan darhol qayta yuboramiz va eslab qolamiz.
            _LOG_GROUP_MIGRATED_ID = e.migrate_to_chat_id
            logger.warning("send_paperwork_to_log_group: log group migrated %s → %s — retrying",
                           target_group, e.migrate_to_chat_id)
            await bot.send_document(
                e.migrate_to_chat_id,
                BufferedInputFile(file_bytes, filename=file_name or "document.pdf"),
                caption=caption,
                parse_mode="HTML",
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
        data = await message_queue.get()
        try:
            message_text = _linkify_maps(data.message)
            # Paperwork-issue notify (Accept/Resend tugmalari bilan) — internal team /
            # driver guruhga keladigan xabarga asl hujjat xabariga "Open original" link
            # qo'shamiz. Notify payload'ida link bo'lmaydi; uni RefNumber bo'yicha
            # cache'dan topamiz (paperwork pipeline hujjatni ishlaganda yozib qo'ygan).
            if data.inline_buttons and any(
                (btn.callback_data or "").startswith("pw_accept_")
                for row in data.inline_buttons for btn in row
            ):
                first_line = data.message.split("\n", 1)[0]
                if "#" in first_line:
                    ref = first_line.split("#", 1)[1].strip()
                    link = PAPERWORK_MSG_LINKS.get(ref)
                    if link:
                        message_text += f'\n\n🔗 <a href="{link}">Open original</a>'
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
                # aiohttp (async) — sync `requests` event-loop'ni bloklardi; audit v3 #3.
                timeout = aiohttp.ClientTimeout(total=20, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(f"{TELEGRAM_API_BASE}/sendMessage", json=send_payload) as send_response:
                        send_result = await send_response.json()
                    if send_result.get("ok"):
                        message_id = send_result["result"]["message_id"]
                        logger.info("📨 Message sent to group %s: %s...", data.group_id, data.message[:50])
                        if data.has_pin_required:
                            pin_payload = {"chat_id": data.group_id, "message_id": message_id,
                                           "disable_notification": False}
                            async with session.post(f"{TELEGRAM_API_BASE}/pinChatMessage", json=pin_payload) as pin_response:
                                pin_result = await pin_response.json()
                            if pin_result.get("ok"):
                                logger.info("📌 Message pinned in group %s", data.group_id)
                    else:
                        await send_error_to_group(f"❌ Error sending message: {send_result.get('description', 'Unknown error')}", group_id=data.group_id)
            except Exception as e:
                await send_error_to_group(f"❌ Error sending message: {e}", group_id=data.group_id)
        except Exception as e:
            await send_error_to_group(f"❌ Worker error: {e}")
            await asyncio.sleep(1)
        finally:
            # EXT-6: task_done() har doim get()'ga mos kelsin (exception bo'lsa ham) — queue hisobi drift qilmasin.
            message_queue.task_done()
