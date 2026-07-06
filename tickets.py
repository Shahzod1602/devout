"""Ticket lifecycle: history forwarding, polling, AI dedupe, priority, swagger POST.

Asosiy oqim:
1. Foydalanuvchi xabar yozadi → handlers.py / message_worker
2. send_to_swagger backend'ga ticket yaratadi, status = "todo"
3. poll_backend_ticket_status backend status'ini polling qiladi
4. Backend done belgilasa, ticket_status_api endpoint chaqiriladi
5. forward_message_to_history_if_todo "todo" oraliqdagi xabarlarni history API'ga yuboradi

Bu modul history API URL'larini hard-code qiladi (https://api.abstract-it.uz/api/tickets/history)
— legacy, FAZA 2 ham hal qilmadi.
"""
import asyncio
import logging
import time
from datetime import datetime

import aiohttp
from aiogram import types
from api.models import TicketStatusRequest, TicketStatusResponse
from config import (
    BASE_URL,
    DEPARTMENT_MAP,
    GROUP_TICKET_NOTIFICATIONS,
    PRIORITY_MAP,
    STATUS_TODO,
    SWAGGER_URL,
    ssl_context,
)
from external import get_api_token, invalidate_token
from groups import save_group_ticket_status
from messaging import send_error_to_group
from state import (
    FAILED_MESSAGES_QUEUE,
    GROUP_TICKET_MESSAGES,
    GROUP_TICKET_POLLING_TASKS,
    GROUP_TICKET_STATUS,
    GROUP_TICKET_TIMERS,
    HISTORY_SENT_MESSAGE_KEYS,
    bot,
    client,
)

logger = logging.getLogger(__name__)

# History endpoint — env-driven via BASE_URL (test: api.abstract-it.uz,
# prod: api.prod.abstract-it.uz, dev: api.dev.abstract-it.uz). Previously
# hardcoded to test, which made prod bot POST to the wrong backend and
# get 404 for every prod group.
_HISTORY_API_URL = f"{BASE_URL}/tickets/history"


# ====== Status check ======

def is_group_ticket_todo(group_id: str | int) -> bool:
    """Group ticket status TODO ekanini tekshiradi."""
    ticket_data = GROUP_TICKET_STATUS.get(str(group_id), {})
    return ticket_data.get("status") == "todo"


# ====== History forwarding ======

def _history_message_key(msg: types.Message) -> str:
    """History API uchun dedup key."""
    return f"{msg.chat.id}:{msg.message_id}"


def history_already_sent(msg: types.Message) -> bool:
    """Dedup + mark: True bo'lsa bu xabar allaqachon history API'ga yuborilgan.

    False qaytganda key mark qilinadi — chaqiruvchi darhol yuborishi kerak.
    TKT-2: key await'dan OLDIN qo'shiladi, redelivery paytida ikki marta POST bo'lmaydi.
    """
    message_key = _history_message_key(msg)
    if message_key in HISTORY_SENT_MESSAGE_KEYS:
        return True
    HISTORY_SENT_MESSAGE_KEYS.add(message_key)
    if len(HISTORY_SENT_MESSAGE_KEYS) > 10000:
        # TKT-8: hammasini clear qilmaymiz (darrov duplikatlar toshmasin) — yarmini saqlaymiz.
        _keep = list(HISTORY_SENT_MESSAGE_KEYS)[5000:]
        HISTORY_SENT_MESSAGE_KEYS.clear()
        HISTORY_SENT_MESSAGE_KEYS.update(_keep)
    return False


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
    """Ticket TODO bo'lsa message'ni history API'ga yuboradi. True = TODO oqimida ishlandi."""
    if not is_group_ticket_todo(msg.chat.id):
        logger.debug("⏭️ [HISTORY] Group %s ticket not TODO, skipping history", msg.chat.id)
        return False

    if history_already_sent(msg):
        logger.debug("⏭️ [HISTORY] Duplicate message %s, skipping", _history_message_key(msg))
        return True

    history_text = _build_history_message_text(msg, fallback_text=fallback_text)
    writer_name = msg.from_user.full_name if msg.from_user else "unknown"
    logger.info("📤 [HISTORY] Sending to history API | group=%s | user=%s | text=%r",
                msg.chat.id, writer_name, history_text[:60])
    # Failure paths already self-report inside send_message_to_history_api:
    #   404 → logger.warning + drop (group not in backend history, expected)
    #   3 retries exhausted → send_error_to_group + FAILED_MESSAGES_QUEUE
    # No need to alert again here.
    await send_message_to_history_api(
        group_id=str(msg.chat.id),
        writer_name=writer_name,
        message=history_text,
    )
    return True


# Outage'da har bir xabar uchun alohida Telegram alert yubormaslik uchun throttle:
# klassifikator endi asosiy trafikni ham history'ga yo'naltiradi (chat yo'li).
_HISTORY_ALERT_INTERVAL = 300  # soniya
_last_history_alert_ts = 0.0


async def send_message_to_history_api(group_id: str, writer_name: str, message: str):
    """Habarni darhol history API'ga yuborish. 3 urinishdan keyin FAILED_MESSAGES_QUEUE'ga qo'shadi."""
    global _last_history_alert_ts
    payload = {
        "groupId": str(group_id),
        "writerName": writer_name,
        "message": message,
    }

    token = await get_api_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "EN",
        "Content-Type": "application/json",
        "X-Group-Id": str(group_id),
    }
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
                async with session.post(
                    _HISTORY_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 201):
                        logger.info("✅ [HISTORY API] Sent | group=%s | writer=%s", group_id, writer_name)
                        return True
                    elif resp.status == 404:
                        logger.warning("⚠️ [HISTORY API] Not found [404] | group=%s | writer=%s | Dropping message", group_id, writer_name)
                        return False
                    elif resp.status in (401, 403):
                        # Token muddati o'tgan — yangilab, keyingi urinishда yangi token bilan.
                        # Avval eski token bilan 2 marta retry qilib, keyin queue'ga tashlardi.
                        logger.warning("⚠️ [HISTORY API] Auth [%s] | group=%s | attempt=%d/3 | refreshing token",
                                       resp.status, group_id, attempt + 1)
                        invalidate_token()
                        token = await get_api_token()
                        headers["Authorization"] = f"Bearer {token}"
                    else:
                        resp_text = await resp.text()
                        logger.warning("⚠️ [HISTORY API] Error [%s] | group=%s | attempt=%d/3 | response=%s",
                                       resp.status, group_id, attempt + 1, resp_text[:200])
        except TimeoutError:
            logger.warning("⚠️ [HISTORY API] Timeout | group=%s | attempt=%d/3", group_id, attempt + 1)
        except Exception as e:
            logger.warning("⚠️ [HISTORY API] Exception | group=%s | attempt=%d/3 | error=%s", group_id, attempt + 1, e)

        if attempt < 2:
            await asyncio.sleep(2 ** attempt)

    now_ts = time.monotonic()
    if now_ts - _last_history_alert_ts >= _HISTORY_ALERT_INTERVAL:
        _last_history_alert_ts = now_ts
        await send_error_to_group(f"❌ [HISTORY API] All 3 attempts failed | writer={writer_name} | Adding to retry queue", group_id=group_id)
    else:
        logger.warning("❌ [HISTORY API] All 3 attempts failed | group=%s | writer=%s | queued (alert throttled)", group_id, writer_name)
    FAILED_MESSAGES_QUEUE.append(payload)
    return False


async def retry_failed_messages():
    """Har 30 soniyada muvaffaqiyatsiz xabarlarni qayta yuborish."""
    while True:
        await asyncio.sleep(30)
        if not FAILED_MESSAGES_QUEUE:
            continue

        retry_list = FAILED_MESSAGES_QUEUE.copy()
        FAILED_MESSAGES_QUEUE.clear()

        logger.info("🔁 Retrying %d failed message(s)...", len(retry_list))
        for payload in retry_list:
            try:
                token = await get_api_token()
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept-Language": "EN",
                    "Content-Type": "application/json",
                    "X-Group-Id": str(payload['groupId']),
                }
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
                    async with session.post(
                        _HISTORY_API_URL,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status in (200, 201):
                            logger.info("✅ Retry successful for group %s", payload['groupId'])
                        elif resp.status == 404:
                            logger.warning("⚠️ Retry failed [404] for group %s. Dropping message.", payload['groupId'])
                        elif resp.status in (401, 403):
                            # Token muddati o'tgan — invalidate qilamiz (keyingi tsikl yangi token oladi) + re-queue.
                            logger.warning("⚠️ Retry auth [%s] for group %s — refreshing token, re-queuing.", resp.status, payload['groupId'])
                            invalidate_token()
                            FAILED_MESSAGES_QUEUE.append(payload)
                        else:
                            logger.warning("⚠️ Retry failed [%s] for group %s. Re-queuing.", resp.status, payload['groupId'])
                            FAILED_MESSAGES_QUEUE.append(payload)
            except Exception as e:
                logger.warning("⚠️ Retry error for group %s: %s. Re-queuing.", payload['groupId'], e)
                FAILED_MESSAGES_QUEUE.append(payload)


async def send_collected_messages_to_api(group_id: str):
    """Placeholder — hozir habarlar darhol API'ga yuboriladi."""
    logger.debug("ℹ️ All messages sent directly to API for group %s", group_id)


# ====== Status lifecycle ======

async def reset_group_ticket_status(group_id: str, delay_minutes: float = 4):
    """N minut keyin group status'ini reset qilish (empty string)."""
    group_id_str = str(group_id)
    try:
        if group_id_str in GROUP_TICKET_TIMERS:
            GROUP_TICKET_TIMERS[group_id_str].cancel()

        async def reset_after_delay():
            await asyncio.sleep(delay_minutes * 60)
            if group_id_str in GROUP_TICKET_STATUS:
                GROUP_TICKET_STATUS[group_id_str]["status"] = ""
                logger.info("✅ Group %s ticket status reset to empty after %s minutes", group_id, delay_minutes)

        task = asyncio.create_task(reset_after_delay())
        GROUP_TICKET_TIMERS[group_id_str] = task
    except Exception as e:
        await send_error_to_group(f"❌ Error setting reset timer for group {group_id}: {e}", group_id=group_id)


async def poll_backend_ticket_status(group_id: str, ticket_id: str | None = None):
    """Backend'da ticket status'ni polling qilish, 'done' bo'lguncha (max 2 soat)."""
    group_id_str = str(group_id)
    poll_interval = 5
    max_polls = 1440  # 2 soat
    poll_count = 0

    try:
        logger.info("🔄 Starting polling for group %s ticket status...", group_id)

        while poll_count < max_polls:
            await asyncio.sleep(poll_interval)
            poll_count += 1

            if group_id_str in GROUP_TICKET_STATUS:
                current_status = GROUP_TICKET_STATUS[group_id_str].get("status", "")

                if current_status == "done":
                    logger.info("✅ Group %s ticket status changed to 'done'. Sending collected messages...", group_id)
                    await send_collected_messages_to_api(group_id)
                    save_group_ticket_status(group_id, "done")

                    if group_id_str in GROUP_TICKET_POLLING_TASKS:
                        del GROUP_TICKET_POLLING_TASKS[group_id_str]

                    break

                if poll_count % 20 == 0:
                    msg_count = len(GROUP_TICKET_MESSAGES.get(group_id_str, []))
                    logger.debug("⏳ Polling group %s: status='todo', collected %d messages", group_id, msg_count)
        else:
            # TKT-4: max_polls'ga 'done'siz yetdi (timeout / o'tkazib yuborilgan webhook).
            # Guruh 'todo'da abadiy qolib history forwarding to'xtamasligi uchun reset qilamiz.
            logger.warning("⏱️ Group %s ticket polling timed out (%d polls) — resetting status", group_id, max_polls)
            GROUP_TICKET_STATUS.pop(group_id_str, None)
            save_group_ticket_status(group_id, "")

    except asyncio.CancelledError:
        logger.warning("⚠️ Polling cancelled for group %s", group_id)
    except Exception as e:
        await send_error_to_group(f"❌ Error during polling for group {group_id}: {e}", group_id=group_id)
    finally:
        if group_id_str in GROUP_TICKET_POLLING_TASKS:
            del GROUP_TICKET_POLLING_TASKS[group_id_str]


async def ticket_status_api(data: TicketStatusRequest):
    """Backend webhook: ticket status update ("done" yoki "")."""
    try:
        group_id_str = str(data.group_id)
        status = data.status.strip().lower()

        if status == "done":
            GROUP_TICKET_STATUS[group_id_str] = {
                "status": "done",
                "created_at": GROUP_TICKET_STATUS.get(group_id_str, {}).get("created_at", datetime.now().isoformat()),
                "done_at": datetime.now().isoformat(),
            }
            logger.info("✅ Group %s ticket status set to 'done'", group_id_str)

            await send_collected_messages_to_api(group_id_str)
            save_group_ticket_status(group_id_str, "done")

            if group_id_str in GROUP_TICKET_POLLING_TASKS:
                polling_task = GROUP_TICKET_POLLING_TASKS[group_id_str]
                polling_task.cancel()
                del GROUP_TICKET_POLLING_TASKS[group_id_str]
                logger.info("⛔ Polling task cancelled for group %s", group_id_str)

            # 1 soniya keyin status'ni reset qilish (0.0167 daqiqa ≈ 1 soniya)
            asyncio.create_task(reset_group_ticket_status(group_id_str, delay_minutes=0.0167))

            return TicketStatusResponse(
                success=True,
                message=f"Group {group_id_str} status set to 'done'. Messages sent to API. Will reset in 1 second.",
            )
        elif status == "" or status == "empty":
            if group_id_str in GROUP_TICKET_STATUS:
                GROUP_TICKET_STATUS[group_id_str]["status"] = ""
            logger.info("✅ Group %s ticket status set to empty", group_id_str)

            return TicketStatusResponse(
                success=True,
                message=f"Group {group_id_str} status set to empty",
            )
        else:
            return TicketStatusResponse(
                success=False,
                message=f"Invalid status: {data.status}. Use 'done' or empty string",
            )
    except Exception as e:
        await send_error_to_group(f"❌ Error in ticket_status_api: {e}")
        return TicketStatusResponse(
            success=False,
            message=f"Error: {str(e)}",
        )


# ====== AI helpers (priority) ======

async def detect_priority(text: str):
    """OpenAI orqali xabar prioritetini aniqlash (high/medium/low)."""
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
            temperature=0.1,
        )
        priority = (res.choices[0].message.content or "").strip().lower()
        if priority in ["high", "medium", "low"]:
            return priority
        return "medium"
    except Exception:
        logger.exception("❌ Priority detection error")
        return "medium"


# ====== Ticket creation (backend POST) ======

async def create_message_link(msg: types.Message):
    """Telegram xabariga deep-link yaratish (private/group/supergroup)."""
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
    """Backend'ga yangi ticket POST qilish. Muvaffaqiyatli bo'lsa polling boshlanadi."""
    token = await get_api_token()
    if not token:
        return False, "Token olib bo'lmadi"

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
        "attachments": [],
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "EN",
        "Content-Type": "application/json",
        "X-Group-Id": str(groupId),
    }

    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(SWAGGER_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                response_text = await resp.text()

                if resp.status in (200, 201):
                    logger.info("✅ Ticket sent successfully to %s with priority %s", department, priority)

                    group_id_str = str(groupId)
                    GROUP_TICKET_STATUS[group_id_str] = {
                        "status": "todo",
                        "created_at": datetime.now().isoformat(),
                        "done_at": None,
                    }
                    logger.info("📋 Group %s ticket status set to 'todo'", groupId)

                    save_group_ticket_status(groupId, "todo")

                    if group_id_str not in GROUP_TICKET_POLLING_TASKS:
                        polling_task = asyncio.create_task(poll_backend_ticket_status(group_id_str))
                        GROUP_TICKET_POLLING_TASKS[group_id_str] = polling_task
                    logger.info("🔄 Started polling task for group %s", groupId)

                    if GROUP_TICKET_NOTIFICATIONS and department != "chat":
                        notification_text = f"📋 ✅ Ticket successfully sent to {department}"
                        try:
                            await bot.send_message(chat_id=groupId, text=notification_text)
                        except Exception:
                            logger.debug("Couldn't post ticket-sent notification to group %s", groupId, exc_info=True)

                    return True, response_text

                elif resp.status == 404:
                    logger.warning("⚠️ Swagger error [404]: %s", response_text)
                    return False, f"Driver not assigned to group: {response_text}"

                elif resp.status in (401, 403):
                    logger.warning("⚠️ Swagger error [%s]: %s", resp.status, response_text)
                    invalidate_token()
                    await get_api_token()
                    return False, f"Session expired: {response_text}"

                elif resp.status == 400:
                    logger.warning("⚠️ Swagger error [400]: %s", response_text)
                    return False, f"Bad request: {response_text}"

                else:
                    # Tuzatildi: avval `group_id` yozilgan edi (NameError) — parametr nomi `groupId`.
                    await send_error_to_group(
                        f"❌ Swagger error [{resp.status}]: {response_text}",
                        group_id=groupId,
                    )
                    return False, response_text

    except Exception as e:
        logger.exception("❌ Error sending ticket")
        return False, str(e)
