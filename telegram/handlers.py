"""Aiogram handlers — Router'ga ro'yxatdan o'tib main.py'da dp.include_router orqali yuklanadi.

State va funksiyalar to'g'ridan-to'g'ri o'z modullaridan import qilinadi —
hech qanday `import bot as _b` yoki late-binding kerak emas.
"""
import logging

import aiohttp
import aiosqlite
from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from checkin import build_checkin_checkout_text, process_checkin_checkout_text
from config import DB_PATH, DEFAULT_QUICK_BUTTONS
from cooldown import check_driver_cooldown, update_conversation_time
from db import get_company_permissions
from external import get_api_token
from groups import (
    check_group_registered_force,
    get_group_driver,
    get_or_fetch_company_id,
    get_team_driver,
    is_any_driver,
    is_internal_group,
    mark_group_started,
    remove_group_token,
    remove_team_driver,
    save_driver_id,
    save_group_company_id,
    save_group_token,
    save_started_groups,
    save_team_driver_id,
    validate_bot_token,
    validate_bot_token_internal,
    wait_for_server_and_check,
)
from messaging import send_action_log, send_error_to_group
from paperwork_pipeline import (
    _send_image_prompt,
    build_pdf_from_images,
    classify_message,
    run_bol_check,
    summarize_text,
)
from state import (
    AWAITING_TOKEN,
    GROUP_DRIVER_IDS,
    GROUP_IMAGE_DEBOUNCE_TASKS,
    GROUP_IMAGE_TIMEOUT_TASKS,
    GROUP_PENDING_IMAGES,
    GROUP_TICKET_STATUS,
    REGISTERED_GROUPS,
    STARTED_GROUPS,
    TOKEN_FAILED_ATTEMPTS,
    bot,
    client,
)
from tickets import (
    create_message_link,
    detect_priority,
    forward_message_to_history_if_todo,
    send_message_to_history_api,
    send_to_swagger,
)
from ui import build_quickbuttons_keyboard, get_quickbuttons

logger = logging.getLogger(__name__)
router = Router()


# ====== Chat lifecycle ======

@router.my_chat_member()
async def on_my_chat_member(event: types.ChatMemberUpdated):
    """Bot guruhdan chiqarilsa yoki guruh o'chirilsa — local ma'lumotlarni tozalash."""
    new_status = event.new_chat_member.status
    if new_status not in ("kicked", "left", "banned"):
        return

    group_id = event.chat.id
    group_id_str = str(group_id)
    logger.info("🗑️ Bot removed from group %s (status=%s), cleaning up...", group_id, new_status)

    remove_group_token(group_id)

    REGISTERED_GROUPS.pop(group_id, None)
    REGISTERED_GROUPS.pop(group_id_str, None)
    AWAITING_TOKEN.pop(group_id, None)
    AWAITING_TOKEN.pop(group_id_str, None)
    GROUP_DRIVER_IDS.pop(group_id_str, None)
    if group_id in STARTED_GROUPS:
        STARTED_GROUPS.discard(group_id)
        save_started_groups()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM groups WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM loads WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM bols WHERE group_id=?", (group_id_str,))
        await db.execute("DELETE FROM pods WHERE group_id=?", (group_id_str,))
        await db.commit()

    logger.info("✅ Cleanup done for group %s", group_id)


# ====== /start + driver setup ======

@router.message(Command("start"))
async def start_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    chat_name = msg.chat.title or msg.from_user.full_name or "Private Chat"

    await msg.answer("🔎 Checking group registration... Please wait.")
    access_token = await get_api_token()
    if not access_token:
        await msg.answer("❌ API connection failed. Please try again later.")
        return

    registered = await check_group_registered_force(chat_id, chat_name, force_check=True)

    if registered:
        AWAITING_TOKEN.pop(chat_id, None)
        TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)
        mark_group_started(chat_id)
        await msg.answer("✅ Group is registered! Bot is ready to use.")
        await send_action_log(chat_id, f"Group checked: {chat_name}")

        # Internal team groups: no drivers, no quick buttons — just confirm.
        if is_internal_group(chat_id):
            return

        if is_any_driver(chat_id, msg.from_user.id):
            buttons = await get_quickbuttons(chat_id)
            keyboard = build_quickbuttons_keyboard(buttons)
            await msg.answer("Quick buttons loaded:", reply_markup=keyboard)
        else:
            kb = InlineKeyboardBuilder()
            kb.button(text="👤 I am a driver", callback_data="set_driver")
            await msg.answer("Bot is ready to use. Quick buttons are only available for drivers.",
                             reply_markup=kb.as_markup())
    else:
        AWAITING_TOKEN[chat_id] = msg.from_user.id
        TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)
        await msg.answer("❌ Group is not registered. Please send the admin token (single line).")


@router.callback_query(F.data == "set_driver")
async def set_driver_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    driver_id = callback.from_user.id
    chat_name = callback.message.chat.title or callback.from_user.full_name or "Private Chat"

    if is_internal_group(chat_id):
        await callback.answer()  # ack silently
        return

    registered = await wait_for_server_and_check(chat_id, chat_name, callback.message, force_check=True)
    if not registered:
        await callback.answer("❌ Group is not registered.", show_alert=True)
        await callback.message.answer("❌ Group is not registered. Please use /start to register first.")
        return

    if is_any_driver(chat_id, driver_id):
        await callback.answer("✅ You are already set as a driver.", show_alert=False)
        return

    primary = get_group_driver(chat_id)
    if primary is None:
        await save_driver_id(chat_id, driver_id, callback.from_user.full_name)
        await send_action_log(chat_id, f"Driver set: {callback.from_user.full_name}")
        await callback.answer("✅ You are now set as a driver.", show_alert=False)
    elif get_team_driver(chat_id) is None:
        await save_team_driver_id(chat_id, driver_id, callback.from_user.full_name)
        await send_action_log(chat_id, f"Team driver set: {callback.from_user.full_name}")
        await callback.answer("✅ You are now set as a team driver.", show_alert=False)
    else:
        await callback.answer("❌ This group already has 2 drivers. Use /teamdriver to replace.", show_alert=True)
        return

    buttons = await get_quickbuttons(chat_id)
    keyboard = build_quickbuttons_keyboard(buttons)
    await callback.message.answer("Quick buttons loaded:", reply_markup=keyboard)


# ====== /internal_team — Internal team group registration ======

@router.message(Command("internal_team"))
async def internal_team_cmd(msg: types.Message, command: CommandObject):
    """/internal_team <TOKEN> — guruhni Internal team sifatida (AgentBot uchun) ro'yxatdan o'tkazadi.

    /start dan farqi: token bitta xabarda komanda bilan birga keladi —
    AWAITING_TOKEN state'i ishlatilmaydi. Backend
    `POST /general-settings/validate-bot-token/internal` ga so'rov yuboriladi.
    """
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    chat_name = msg.chat.title or msg.from_user.full_name or "Private Chat"

    token = (command.args or "").strip()
    if not token:
        await msg.answer(
            "❌ Usage: <code>/internal_team &lt;TOKEN&gt;</code>\n"
            "Example: <code>/internal_team MzozOjIyZmU0YzRlLWVkZWMtNGQxMC1iYjk3...</code>"
        )
        return

    await msg.answer("🔐 Validating internal team token...")
    access_token = await get_api_token()
    if not access_token:
        await msg.answer("❌ API connection failed. Please try again later.")
        return

    result = await validate_bot_token_internal(access_token, token, chat_id, chat_name)
    if result.get("success"):
        save_group_token(chat_id, token, chat_name, group_type="internal")
        AWAITING_TOKEN.pop(chat_id, None)
        TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)

        try:
            resp_data = result.get("data") or {}
            company_id = resp_data.get("companyId") or resp_data.get("company_id")
            if company_id:
                save_group_company_id(chat_id, company_id)
                logger.info("💾 companyId %s saved after internal token validation for group %s", company_id, chat_id)
        except (AttributeError, TypeError):
            logger.debug("Internal token validation result has no parseable company_id", exc_info=True)

        await check_group_registered_force(chat_id, chat_name, force_check=True)
        mark_group_started(chat_id)
        await msg.answer("🎉 Group successfully registered as Internal Team!")
        await send_action_log(chat_id, f"Internal team registration successful: {chat_name}")
    else:
        error_msg = result.get("message", "Unknown error")
        if isinstance(error_msg, str) and "<" in error_msg and ">" in error_msg:
            error_msg = "Server returned HTML response. Please check if the token is correct."
        await msg.answer(f"❌ Internal token validation failed: {error_msg}")


# ====== /help ======

@router.message(Command("help"))
async def help_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)
    await msg.answer(
        "📋 <b>Available Commands</b>\n\n"
        "/start — Register the group and load quick buttons\n"
        "/internal_team &lt;TOKEN&gt; — Register the group as an Internal Team (AgentBot)\n"
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
        parse_mode="HTML",
    )


# ====== /setdriver ======

@router.message(Command("setdriver"))
async def setdriver_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    if is_internal_group(chat_id):
        return  # internal teams have no drivers
    group_id_str = str(chat_id)

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

    chat_name = msg.chat.title or msg.from_user.full_name or "Group"
    await check_group_registered_force(chat_id, chat_name, force_check=True)
    await msg.answer("✅ Driver set successfully.")


# ====== /teamdriver ======

@router.message(Command("teamdriver"))
async def teamdriver_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    if is_internal_group(chat_id):
        return

    if get_group_driver(chat_id) is None:
        await msg.answer("⛔ No primary driver set. Use /setdriver first.")
        return

    args = msg.text.split()

    if len(args) > 1 and args[1].lower() == "remove":
        if get_team_driver(chat_id) is None:
            await msg.answer("ℹ️ No team driver to remove.")
        else:
            await remove_team_driver(chat_id)
            await send_action_log(chat_id, "Team driver removed")
            await msg.answer("✅ Team driver removed.")
        return

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


# ====== /sleep + /deletesleep ======

@router.message(Command("sleep"))
async def sleep_cmd(msg: types.Message):
    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    if is_internal_group(chat_id):
        return

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


@router.callback_query(F.data.startswith("sleep_"))
async def sleep_callback(callback: types.CallbackQuery):
    from config import SLEEP_TIMER_URL, ssl_context

    chat_id = callback.message.chat.id
    if is_internal_group(chat_id):
        await callback.answer()
        return
    hours = int(callback.data.split("_")[1])

    token = await get_api_token()
    if not token:
        await callback.message.answer("❌ Failed to get API token.")
        await callback.answer()
        return

    payload = {"groupId": str(chat_id), "sleepTimeAmountInHours": hours}
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            headers = {"Authorization": f"Bearer {token}", "X-Group-Id": str(chat_id)}
            async with session.post(SLEEP_TIMER_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 201):
                    logger.info("✅ Sleep timer set for %sh in group %s", hours, chat_id)
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


@router.message(Command("deletesleep"))
async def deletesleep_cmd(msg: types.Message):
    from config import SLEEP_TIMER_URL, ssl_context

    await forward_message_to_history_if_todo(msg)

    chat_id = msg.chat.id
    if is_internal_group(chat_id):
        return

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
    logger.info("🗑️ Deleting sleep timer for group %s, URL: %s, payload: %s", chat_id, SLEEP_TIMER_URL, payload)
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
                logger.info("🗑️ Delete sleep timer response [%s]: %s", resp.status, text)
                if resp.status in (200, 201, 204):
                    await msg.answer("✅ Sleep timer deleted successfully!")
                else:
                    await msg.answer("⚠️ Failed to delete sleep timer. It may not exist.")
    except Exception as e:
        await send_error_to_group(f"❌ Delete sleep timer API exception: {e}", group_id=msg.chat.id)
        await msg.answer("⚠️ Failed to delete sleep timer.")


# ====== Generic text handler (token validation, tickets, check-in/out, etc.) ======

@router.message(F.text)
async def generic_text_handler(msg: types.Message):

    chat_id = msg.chat.id
    chat_name = msg.chat.title or msg.from_user.full_name or "Private Chat"
    text = msg.text.strip()
    user_id = msg.from_user.id

    if chat_id not in STARTED_GROUPS and not AWAITING_TOKEN.get(chat_id):
        return

    # Internal team groups: no drivers. Text handler'da [TODO]-tagged xabarlar
    # `forward_message_to_history_if_todo` orqali backend'ga uzatiladi; backend
    # deletion sync uchun `wait_for_server_and_check` ham chaqiriladi (standard
    # xulq bilan teng). Qolgan oqim (check-in, basket, classify+history, ticket)
    # — hammasi skip.
    _internal = is_internal_group(chat_id)

    if text == "🔄 Refresh":
        if not is_any_driver(chat_id, user_id):
            return
        await send_action_log(chat_id, "Refresh button clicked")

        registered = await wait_for_server_and_check(chat_id, chat_name, msg, force_check=True)
        if not registered:
            await msg.answer("❌ Group is not registered. Please use /start to re-register.")
            return

        buttons = await get_quickbuttons(chat_id)
        keyboard = build_quickbuttons_keyboard(buttons)
        await msg.answer("🔄 Buttons refreshed!", reply_markup=keyboard)
        return

    if is_any_driver(chat_id, user_id) and text in DEFAULT_QUICK_BUTTONS:
        logger.info("🔘 Quick button pressed: %s", text)
        await update_conversation_time(chat_id, user_id)

    if AWAITING_TOKEN.get(chat_id) and AWAITING_TOKEN[chat_id] == user_id:
        await msg.answer("🔐 Token received... validating.")
        access_token = await get_api_token()
        if not access_token:
            await msg.answer("❌ API login failed. Try again later.")
            return

        result = await validate_bot_token(access_token, text, chat_id, chat_name)
        if result.get("success"):
            save_group_token(chat_id, text, chat_name)
            AWAITING_TOKEN.pop(chat_id, None)
            TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)

            try:
                resp_data = result.get("data") or {}
                company_id = resp_data.get("companyId") or resp_data.get("company_id")
                if company_id:
                    save_group_company_id(chat_id, company_id)
                    logger.info("💾 companyId %s saved after token validation for group %s", company_id, chat_id)
            except (AttributeError, TypeError):
                logger.debug("Token validation result has no parseable company_id", exc_info=True)

            await check_group_registered_force(chat_id, chat_name, force_check=True)

            mark_group_started(chat_id)
            await msg.answer("🎉 Group successfully registered and ready to use!")
            await send_action_log(chat_id, f"Group registration successful: {chat_name}")

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
                AWAITING_TOKEN.pop(chat_id, None)
                TOKEN_FAILED_ATTEMPTS.pop(chat_id, None)
            else:
                await msg.answer(
                    f"❌ Token validation failed: {error_msg}\n"
                    "Please send the correct admin token. (1 attempt left before bot stops responding.)"
                )
        return

    if not _internal:
        checkin_text = build_checkin_checkout_text(msg, text)
        checkin_processed = await process_checkin_checkout_text(checkin_text, chat_id, msg)

        if checkin_processed:
            return

    if await forward_message_to_history_if_todo(msg, fallback_text=text):
        return

    # Backend deletion sync: standard guruh xulqi bilan teng — RAM cache hit bo'lsa
    # bepul; miss bo'lsa by-group API → 404 bo'lsa REGISTERED_GROUPS tozalanadi va
    # "Group not found" xabari avtomatik yuboriladi (groups.py:301).
    registered = await wait_for_server_and_check(chat_id, chat_name, msg, force_check=False)
    if not registered:
        REGISTERED_GROUPS.pop(str(chat_id), None)
        return

    # Internal team: classify / history-API / basket / ticket pipeline kerak emas.
    # Faqat [TODO]-tagged xabarlar yuqorida forward bo'ldi.
    if _internal:
        return

    await send_action_log(chat_id, f"Message from {msg.from_user.full_name}: {text[:50]}...")

    try:
        dep = await classify_message(text)
    except Exception:
        logger.exception("❌ classify_message failed")
        dep = "updater"

    if "basket" in text.lower():
        _basket_company_id = await get_or_fetch_company_id(chat_id)
        if _basket_company_id:
            _basket_perms = await get_company_permissions(str(_basket_company_id))
            if _basket_perms and not _basket_perms.get("ticketCreate", True):
                return
        logger.info("🧺 Basket keyword detected - sending to dashboard")
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
            priority=priority,
        )
        if ok:
            logger.info("✅ Basket ticket sent to updater department")
        await msg.answer("🔍 Checking...")
        return

    if dep == "chat":
        await send_message_to_history_api(
            group_id=chat_id,
            writer_name=msg.from_user.full_name,
            message=text,
        )
        logger.info("💬 Chat message sent to history API for group %s", chat_id)
        return

    if not is_any_driver(chat_id, user_id):
        logger.warning("⚠️ Non-driver message. User %s is not a driver. Ticket will not be sent.", user_id)
        return

    group_id_str = str(chat_id)
    if group_id_str in GROUP_TICKET_STATUS:
        ticket_status = GROUP_TICKET_STATUS[group_id_str].get("status")
        if ticket_status == "done":
            return

    if is_any_driver(chat_id, user_id):
        can_send, remaining = await check_driver_cooldown(user_id)
        if not can_send:
            logger.debug("⏳ Driver %s cooldown: %d seconds remaining", user_id, int(remaining))
            return

    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("ticketCreate", True):
            return
    else:
        perms = None

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
        ticket_type=1,
    )


# ====== Pending image callbacks ======

@router.callback_query(F.data == "pending_bol")
async def pending_bol_callback(callback: types.CallbackQuery):
    """Driver BOL tugmasini bosdi — pending PDF ni BOL sifatida tekshir."""
    chat_id = callback.message.chat.id
    if is_internal_group(chat_id):
        await callback.answer()
        return
    group_key = str(chat_id)
    pending = GROUP_PENDING_IMAGES.get(group_key)

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


@router.callback_query(F.data == "pending_docs")
async def pending_docs_callback(callback: types.CallbackQuery):
    """Driver DOCS tugmasini bosdi — pending PDF ni oddiy hujjat sifatida saqlash."""
    chat_id = callback.message.chat.id
    if is_internal_group(chat_id):
        await callback.answer()
        return
    group_key = str(chat_id)
    GROUP_PENDING_IMAGES.pop(group_key, None)
    timeout_task = GROUP_IMAGE_TIMEOUT_TASKS.pop(group_key, None)
    if timeout_task and not timeout_task.done():
        timeout_task.cancel()
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("📄 Document saved.")


# ====== Paperwork issue Accept / Resend (driver group + internal team) ======
#
# Bu callback'lar `/api/paperwork-issue/notify` orqali yuborilgan xabardagi
# Accept / Resend tugmalariga javob beradi. `callback_data` formati:
#   pw_accept_<issueId> / pw_resend_<issueId>
# Standard va internal team guruhlarda BIR XIL ishlaydi — silent-skip yo'q.

async def _clear_markup(message: types.Message) -> None:
    """Inline tugmalarni olib tashlash. Tugma allaqachon yo'q bo'lsa (takroriy
    bosish), Telegram 'message is not modified' xatosini e'tiborsiz qoldiradi."""
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("pw_accept_"))
async def paperwork_accept_callback(callback: types.CallbackQuery):
    """Paperwork-issue Accept tugmasi: backend'ga `/paperwork-issues/{id}/accepted` POST."""
    from config import PAPERWORK_ISSUES_URL, ssl_context

    issue_id = callback.data.removeprefix("pw_accept_")
    chat_id = callback.message.chat.id
    user_name = callback.from_user.full_name or "user"
    reason = (
        "Accepted by updater in the internal team group."
        if is_internal_group(chat_id)
        else "Accepted by updater in the driver group."
    )

    token = await get_api_token()
    if not token:
        await callback.answer("❌ API auth failed", show_alert=True)
        return

    url = f"{PAPERWORK_ISSUES_URL}/{issue_id}/accepted"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "EN",
        "X-Group-Id": str(chat_id),
    }
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(url, json={"reason": reason}, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 201, 204):
                    await _clear_markup(callback.message)
                    await callback.message.reply(f"✅ Accepted by {user_name}")
                    await callback.answer()
                    logger.info("✅ pw_accept %s by %s in group %s", issue_id, user_name, chat_id)
                elif resp.status in (400, 404, 409):
                    # Allaqachon qabul qilingan yoki issue topilmadi — takroriy
                    # bosish. Xato emas: do'stona xabar + tugmalarni olib tashlash.
                    body = await resp.text()
                    logger.info("ℹ️ pw_accept %s already handled [%s]: %s", issue_id, resp.status, body[:200])
                    await _clear_markup(callback.message)
                    await callback.answer("ℹ️ Already accepted", show_alert=True)
                else:
                    body = await resp.text()
                    logger.warning("⚠️ pw_accept %s failed [%s]: %s", issue_id, resp.status, body[:200])
                    await callback.answer(f"❌ Backend {resp.status}", show_alert=True)
    except Exception as e:
        logger.exception("❌ paperwork_accept_callback error")
        await callback.answer(f"❌ {e}", show_alert=True)


@router.callback_query(F.data.startswith("pw_resend_"))
async def paperwork_resend_callback(callback: types.CallbackQuery):
    """Paperwork-issue Resend tugmasi: backend'ga `/paperwork-issues/{id}/resend-document` POST."""
    from config import PAPERWORK_ISSUES_URL, ssl_context

    issue_id = callback.data.removeprefix("pw_resend_")
    chat_id = callback.message.chat.id
    user_name = callback.from_user.full_name or "user"

    token = await get_api_token()
    if not token:
        await callback.answer("❌ API auth failed", show_alert=True)
        return

    url = f"{PAPERWORK_ISSUES_URL}/{issue_id}/resend-document"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept-Language": "EN",
        "X-Group-Id": str(chat_id),
    }
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(url, json={}, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status in (200, 201, 204):
                    await _clear_markup(callback.message)
                    await callback.message.reply(f"🔄 Resend requested by {user_name}")
                    await callback.answer()
                    logger.info("🔄 pw_resend %s by %s in group %s", issue_id, user_name, chat_id)
                elif resp.status in (400, 404, 409):
                    # Allaqachon so'ralgan yoki issue topilmadi — takroriy bosish.
                    # Xato emas: do'stona xabar + tugmalarni olib tashlash.
                    body = await resp.text()
                    logger.info("ℹ️ pw_resend %s already handled [%s]: %s", issue_id, resp.status, body[:200])
                    await _clear_markup(callback.message)
                    await callback.answer("ℹ️ Resend already requested", show_alert=True)
                else:
                    body = await resp.text()
                    logger.warning("⚠️ pw_resend %s failed [%s]: %s", issue_id, resp.status, body[:200])
                    await callback.answer(f"❌ Backend {resp.status}", show_alert=True)
    except Exception as e:
        logger.exception("❌ paperwork_resend_callback error")
        await callback.answer(f"❌ {e}", show_alert=True)


# ====== Documents (BOL/POD) ======

@router.message(F.document | F.photo)
async def handle_documents(msg: types.Message):
    """BOL va POD hujjatlarini qabul qilish va tekshirish (isComplete-based)."""
    import asyncio
    import time

    chat_id = msg.chat.id
    user_id = msg.from_user.id

    if chat_id not in STARTED_GROUPS:
        return

    # Internal team: paperwork (BOL/POD) o'chirilgan — sukut bilan ignore.
    if is_internal_group(chat_id):
        return

    chat_name = msg.chat.title or msg.from_user.full_name or "Group"
    is_registered = await wait_for_server_and_check(chat_id, chat_name, msg, force_check=True)
    if not is_registered:
        await msg.answer("❌ Group is not registered. Please use /start to register first.")
        return

    if msg.caption:
        caption_text = build_checkin_checkout_text(msg, msg.caption)
        await process_checkin_checkout_text(caption_text, chat_id, msg)

    logger.debug("🔍 handle_documents | user_id=%s | primary=%s | team=%s", user_id, get_group_driver(chat_id), get_team_driver(chat_id))
    if not is_any_driver(chat_id, user_id):
        return

    company_id = await get_or_fetch_company_id(chat_id)
    if company_id:
        perms = await get_company_permissions(str(company_id))
        if perms and not perms.get("bolPodPaperworkAnalysis", True):
            return

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

    try:
        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        file_bytes_value = file_bytes.getvalue()

        if is_photo and company_id:
            photo_perms = await get_company_permissions(str(company_id))
            if photo_perms and not photo_perms.get("photoPdf", True):
                return
        if is_photo:
            group_key = str(chat_id)
            now = time.time()
            pending = GROUP_PENDING_IMAGES.get(group_key)

            if pending and (now - pending.get("last_image_time", now)) > 300:
                GROUP_PENDING_IMAGES.pop(group_key, None)
                old_task = GROUP_IMAGE_TIMEOUT_TASKS.pop(group_key, None)
                if old_task and not old_task.done():
                    old_task.cancel()
                pending = None

            pending = GROUP_PENDING_IMAGES.setdefault(group_key, {"pages": [], "prompt_msg_id": None, "last_image_time": now})
            pending["last_image_time"] = now
            pending["pages"].append(file_bytes_value)

            existing_task = GROUP_IMAGE_DEBOUNCE_TASKS.get(group_key)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            GROUP_IMAGE_DEBOUNCE_TASKS[group_key] = asyncio.create_task(
                _send_image_prompt(chat_id, group_key, msg)
            )
            return

        await run_bol_check(chat_id, file_bytes_value, file_name, msg)

    except Exception as e:
        await send_error_to_group(f"❌ Document verification error: {e}", group_id=chat_id)
        logger.exception("❌ Document verification error")
        await msg.answer(f"❌ Document verification failed: {str(e)}")


# ====== Voice / audio (check-in/out from speech) ======

@router.message(F.voice | F.audio)
async def handle_voice(msg: types.Message):
    """Ovozli xabar yoki audio fayldan checkin/checkout parse qilish."""
    import asyncio
    from io import BytesIO

    chat_id = msg.chat.id
    user_id = msg.from_user.id

    if chat_id not in STARTED_GROUPS:
        return

    # Internal team: voice check-in/out o'chirilgan — sukut bilan ignore.
    if is_internal_group(chat_id):
        return

    chat_name = msg.chat.title or msg.from_user.full_name or "Group"
    is_registered = await check_group_registered_force(chat_id, chat_name, force_check=False)
    if not is_registered:
        return

    if not is_any_driver(chat_id, user_id):
        return

    try:
        file_id = msg.voice.file_id if msg.voice else msg.audio.file_id

        file = await bot.get_file(file_id)
        file_bytes = await bot.download_file(file.file_path)
        file_bytes_value = file_bytes.getvalue()

        audio_file = BytesIO(file_bytes_value)
        audio_file.name = "voice.ogg"

        loop = asyncio.get_event_loop()
        transcript = await loop.run_in_executor(
            None,
            lambda: client.audio.transcriptions.create(model="whisper-1", file=audio_file),
        )
        text = transcript.text.strip()
        logger.info("🎤 Voice transcribed: %s", text)

        if not text:
            return

        voice_checkin_text = build_checkin_checkout_text(msg, text)
        await process_checkin_checkout_text(voice_checkin_text, chat_id, msg)

    except Exception as e:
        await send_error_to_group(f"❌ Voice handler error: {e}", group_id=chat_id)
        logger.exception("❌ Voice handler error")
