"""High-level paperwork pipeline: image batching → PDF → /check-bol → backend POST.

Bu modul Telegram handler'larini paperwork/ package bilan bog'laydi:
- summarize_text, classify_message: OpenAI orqali yordamchi NLP (chat triage)
- build_pdf_from_images: ko'p rasmni bitta PDF'ga jamlash
- _send_image_prompt: 1.5s debounce, "Analyze" tugmasi
- run_bol_check: /check-bol endpoint'ga POST, javobni qayta ishlash, BOL/POD/Late Slip
  oqimi bo'yicha backend'ga paperwork issue yuborish

`/check-bol` o'zi `paperwork/api.py`'da (FastAPI route); bu modul faqat Telegram
handler'lari uchun client.
"""
import asyncio
import logging
from io import BytesIO

import httpx
from aiogram import types
from config import BOT_PORT
from db import (
    add_bol_to_cache,
    add_pod_to_cache,
    get_bols_count,
    get_delivery_count,
    get_last_bol,
    get_load_from_cache,
    get_pickup_count,
    get_pods_count,
    init_load_in_cache,
)
from external import post_paperwork_issue, verify_delivery
from messaging import send_error_to_group, send_paperwork_to_log_group
from paperwork import (
    analyze_big_box_pod_gemini,
    count_stops_by_type,
    determine_file_type,
    is_big_box_delivery,
    process_file,
)
from PIL import Image
from state import GROUP_IMAGE_TIMEOUT_TASKS, GROUP_PENDING_IMAGES, bot, client

logger = logging.getLogger(__name__)


# ====== Lightweight LLM helpers ======

async def summarize_text(text: str):
    """6+ so'zli matnni qisqacha yig'ish (OpenAI gpt-4o-mini). Xato bo'lsa originalni qaytaradi."""
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
            temperature=0.3,
        )
        return (res.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("❌ Summarize error")
        return text


async def classify_message(text: str):
    """Xabarni 8 ta department'dan biriga klassifikatsiya (default: "updater")."""
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
            temperature=0.1,
        )
        classification = (res.choices[0].message.content or "").strip().lower()
        valid_categories = ["chat", "dispatcher", "fleet", "safety", "insurance", "hr", "accounting", "updater"]
        return classification if classification in valid_categories else "updater"
    except Exception:
        logger.exception("❌ Classify error")
        return "updater"


# ====== Image batching ======

def build_pdf_from_images(pages: list) -> bytes:
    """Bir nechta rasm byteslaridan bitta PDF yasaydi (RGB ga konvertatsiya bilan)."""
    imgs: list[Image.Image] = []
    for page_bytes in pages:
        img: Image.Image = Image.open(BytesIO(page_bytes))
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        imgs.append(img)
    buf = BytesIO()
    imgs[0].save(buf, format='PDF', save_all=True, append_images=imgs[1:])
    return buf.getvalue()


async def _auto_remove_analyze_button(chat_id: int, group_key: str, message_id: int):
    """Placeholder — tugma endi avtomatik o'chirilmaydi (5 daqiqadan keyin yangi rasm guruh
    sifatida ishlanadi)."""
    pass


async def _send_image_prompt(chat_id: int, group_key: str, msg: types.Message):
    """1.5s debounce: hamma rasmlar yig'ilgandan keyin bitta PDF prompt chiqaradi."""
    await asyncio.sleep(1.5)
    pending = GROUP_PENDING_IMAGES.get(group_key)
    if not pending or not pending["pages"]:
        return

    if pending.get("prompt_msg_id"):
        try:
            await bot.delete_message(chat_id, pending["prompt_msg_id"])
        except Exception:
            logger.debug("Couldn't delete old prompt msg %s in chat %s", pending["prompt_msg_id"], chat_id, exc_info=True)
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
        reply_markup=kb,
    )
    pending["prompt_msg_id"] = sent.message_id

    existing_timeout = GROUP_IMAGE_TIMEOUT_TASKS.get(group_key)
    if existing_timeout and not existing_timeout.done():
        existing_timeout.cancel()
    GROUP_IMAGE_TIMEOUT_TASKS[group_key] = asyncio.create_task(
        _auto_remove_analyze_button(chat_id, group_key, sent.message_id)
    )


# ====== Main pipeline ======

async def run_bol_check(chat_id: int, file_bytes_value: bytes, file_name: str, msg: types.Message, answer_msg=None):
    """PDF ni local /check-bol endpoint'ga yuborib, javobga qarab BOL/POD/Late Slip oqimini bajarish.

    Har bir hujjat — selected (tahlil qilindi) bo'lsin, skipped (o'tkazib yuborildi)
    bo'lsin — natijasi va sababi bilan PAPERWORK_LOG_GROUP_ID guruhiga forward
    qilinadi. Asl ish mantig'i `_run_bol_check_impl` ichida; bu wrapper faqat
    (status, reason, load_id) ni olib, faylni log guruhga yuboradi.
    """
    status, reason, load_id = "skipped", "Unknown outcome", None
    try:
        status, reason, load_id = await _run_bol_check_impl(
            chat_id, file_bytes_value, file_name, msg, answer_msg
        )
    except Exception as e:
        reason = f"Unexpected error: {e}"
        raise
    finally:
        await send_paperwork_to_log_group(
            file_bytes_value, file_name, status, reason, chat_id=chat_id, load_id=load_id,
        )


async def _run_bol_check_impl(chat_id: int, file_bytes_value: bytes, file_name: str, msg: types.Message, answer_msg=None):
    """run_bol_check ichki logikasi. (status, reason, load_id) qaytaradi.

    status: "selected" | "skipped". Har bir terminal nuqtada aniq sabab qaytaradi.
    """
    checking_msg = answer_msg or await msg.answer("🔍 Checking document...")

    files = {'bol': (file_name, file_bytes_value, 'application/octet-stream')}
    data = {'group_id': str(chat_id)}

    logger.debug("🔵 run_bol_check: sending POST to /check-bol...")
    try:
        async with httpx.AsyncClient() as client_http:
            response = await client_http.post(
                f"http://localhost:{BOT_PORT}/check-bol",
                data=data, files=files, timeout=180,
            )
            logger.debug("🔵 run_bol_check: POST returned status=%s", response.status_code)
            if response.status_code == 422:
                await send_error_to_group(f"❌ check-bol 422 error: {response.text}", group_id=chat_id)
                await checking_msg.delete()
                await msg.answer("❌ Document check failed (invalid request). Please try again.")
                return "skipped", "check-bol 422 (invalid request)", None
            logger.debug("🔵 HTTP %s received, parsing JSON...", response.status_code)
            check_result = response.json()
            logger.debug("🔵 JSON parsed OK: %s", list(check_result.keys()))
    except httpx.TimeoutException:
        await send_error_to_group("❌ run_bol_check TIMEOUT", group_id=chat_id)
        await checking_msg.delete()
        await msg.answer("⏱️ Document check timed out. Please try again.")
        return "skipped", "check-bol timed out", None
    except Exception as e:
        await send_error_to_group(f"❌ run_bol_check EXCEPTION: {e}", group_id=chat_id)
        await checking_msg.delete()
        await msg.answer(f"❌ Document check failed: {e}")
        return "skipped", f"check-bol request failed: {e}", None

    logger.info("📋 check-bol API response: success=%s, message=%s",
                check_result.get('success'), check_result.get('message', 'N/A')[:100])

    if check_result.get('success'):
        _db_id = check_result.get('loadDbId')
        new_load_id = _db_id if _db_id is not None else check_result.get('loadId', 'N/A')
        load_display_id = check_result.get('loadId', new_load_id)

        stops = check_result.get('stops', [])
        is_late_slip = bool(check_result.get('isLateSlip', False))
        file_type = determine_file_type(stops)
        logger.info("📋 determine_file_type → %s for load #%s (isLateSlip=%s)", file_type, load_display_id, is_late_slip)

        if is_late_slip:
            file_type = 4
            logger.info("📮 Late Slip aniqlandi → file_type=4 for load #%s", load_display_id)

        # Agar stops'da pickup'lar complete ko'rinsa (file_type=2) lekin BOL hali DB'da
        # yo'q bo'lsa, bu birinchi hujjat — BOL sifatida qabul qilamiz.
        if file_type == 2 and 'paperwork_result' in check_result:
            existing_bol = await get_last_bol(chat_id, new_load_id)
            if not existing_bol:
                logger.warning("⚠️ determine_file_type=2 but no BOL in DB yet → treating as BOL (file_type=1)")
                file_type = 1
            else:
                logger.info("✅ BOL already in DB → keeping file_type=2 (POD)")

        pickup_count, delivery_count = count_stops_by_type({"stops": stops})
        load_data = await get_load_from_cache(chat_id, new_load_id)
        if not load_data:
            await init_load_in_cache(chat_id, new_load_id, pickup_count, delivery_count)

        if file_type == -1:
            await checking_msg.delete()
            await msg.answer(f"ℹ️ Load #{load_display_id} is already complete. No document needed.")
            return "skipped", "Load already complete — no document needed", load_display_id

        if file_type == 0:
            file_type = 1

        if file_type == 4:
            await checking_msg.delete()
            paperwork = check_result.get('paperwork_result', {})
            if not paperwork:
                await msg.answer(f"❌ Late Slip uchun paperwork data topilmadi (Load #{load_display_id})")
                return "skipped", "Late Slip: no paperwork data", load_display_id
            late_post_result = await post_paperwork_issue(
                paperwork, file_bytes_value, file_name or "late_slip_document",
                file_type=4, group_id=chat_id, message_id=msg.message_id,
            )
            if late_post_result["success"]:
                logger.info("✅ Late Slip paperwork issue yuborildi! Load #%s", new_load_id)
                await msg.answer(f"✅ Load #{load_display_id} Late Slip received!")
                return "selected", "Late Slip received", load_display_id
            else:
                await send_error_to_group(f"❌ Late Slip paperwork issue xatosi: {late_post_result.get('error')}", group_id=chat_id)
                await msg.answer(f"❌ Late Slip yuborilmadi (Load #{load_display_id}). Please try again.")
                return "skipped", f"Late Slip post failed: {late_post_result.get('error')}", load_display_id

        if file_type == 1:
            await add_bol_to_cache(chat_id, new_load_id, msg.message_id, file_bytes_value)
            bols_count = await get_bols_count(chat_id, new_load_id)
            required_bols = await get_pickup_count(chat_id, new_load_id)
            await checking_msg.delete()
            await msg.answer(f"✅ Load #{load_display_id} BOL #{bols_count}/{required_bols} received!")

            logger.debug("📋 check_result keys: %s", list(check_result.keys()))
            if 'paperwork_result' in check_result:
                paperwork = check_result['paperwork_result']
                logger.debug("📋 paperwork loadId=%s, calling post_paperwork_issue...", paperwork.get('loadId'))
                bol_post_result = await post_paperwork_issue(
                    paperwork, file_bytes_value, file_name or "bol_document",
                    file_type=1, group_id=chat_id, message_id=msg.message_id,
                )
                if bol_post_result["success"]:
                    logger.info("✅ BOL paperwork issue yuborildi! Load #%s", new_load_id)
                else:
                    await send_error_to_group(f"❌ BOL paperwork issue xatosi: {bol_post_result.get('error')}", group_id=chat_id)

            return "selected", f"BOL #{bols_count}/{required_bols} received", new_load_id

        elif file_type == 2:
            await checking_msg.delete()
            bol_bytes = await get_last_bol(chat_id, new_load_id)
            if not bol_bytes:
                logger.warning("⚠️ No BOL on file for group=%s, load=%s", chat_id, new_load_id)
                return "skipped", "POD received but no BOL on file yet", new_load_id

            result = await verify_delivery(bol_bytes, file_bytes_value)
            if result.get("success"):
                await add_pod_to_cache(chat_id, new_load_id, msg.message_id, file_bytes_value)
                pods_count = await get_pods_count(chat_id, new_load_id)
                required_pods = await get_delivery_count(chat_id, new_load_id)

                verify_data = result.get("data", {})
                address_match = verify_data.get("address_match", False)
                pod_valid = verify_data.get("pod_valid", False)
                address_notes = verify_data.get("address_notes", "")[:200]
                pod_notes = verify_data.get("pod_notes", "")[:200]

                analysis_failed = bool(verify_data.get("analysis_failed"))
                bol_pages = verify_data.get("bol_pages")
                pod_pages = verify_data.get("pod_pages")
                # AskAI sahifalar soni teng bo'lmasa tahlilni o'tkazib yuborib, faqat
                # umumiy `notes` qaytaradi — bu xabar faqat Page count kriteriyasiga
                # tegishli, signature/address kriteriyalariga sizib chiqmasligi kerak.
                page_mismatch = (
                    not analysis_failed
                    and bol_pages is not None and pod_pages is not None
                    and bol_pages != pod_pages
                )

                if page_mismatch:
                    # Page count teng emas — signature/address tahlili o'tkazib
                    # yuborilgan. Bu kriteriyalarni xatosiz (neytral) qoldiramiz,
                    # muammo faqat Page count bo'limida ko'rinsin (boshqa joyga
                    # sizib chiqmasin).
                    address_match = True
                    pod_valid = True
                    address_notes = "—"
                    pod_notes = "—"

                # AskAI couldn't parse the document → show a friendly note instead of
                # leaking a raw error (e.g. "Error: Invalid JSON response") into the card.
                if analysis_failed or address_notes.startswith("Error:"):
                    address_notes = "Could not analyze — please Resend."
                if analysis_failed or pod_notes.startswith("Error:"):
                    pod_notes = "Could not analyze — please Resend."

                pod_paperwork_data = {
                    "loadId": new_load_id,
                    "weight": {"isHealthy": True, "summary": "N/A"},
                    "signature": {"isHealthy": pod_valid, "summary": pod_notes if pod_notes else ("Found" if pod_valid else "Not found")},
                    "poNumber": {"isHealthy": True, "summary": "N/A"},
                    "pickUpAddress": {"isHealthy": address_match, "summary": address_notes if address_notes else ("Match" if address_match else "Mismatch")},
                    "deliveryAddressAddress": {"isHealthy": address_match, "summary": address_notes if address_notes else ("Match" if address_match else "Mismatch")},
                }
                if bol_pages is not None and pod_pages is not None:
                    pod_paperwork_data["pageCount"] = {
                        "isHealthy": not page_mismatch,
                        "summary": (
                            f"BOL has {bol_pages} page(s), POD has {pod_pages} page(s). They must be equal."
                            if page_mismatch else str(pod_pages)
                        ),
                    }

                # Costco / Walmart / Target ga yetkazib berishda broker POD bilan birga
                # Trailer Control Record, Delivery Report va Sticker hujjatlarini talab
                # qiladi. Delivery stop shu retailerlardan biri bo'lsa — POD yuklamasini
                # Gemini bilan tahlil qilib, 3 kategoriyani paperwork issue'ga qo'shamiz.
                is_big_box, retailer = is_big_box_delivery(stops)
                if is_big_box:
                    logger.info("📦 Load #%s delivery → %s, qo'shimcha hujjatlar tekshirilmoqda...",
                                load_display_id, retailer)
                    try:
                        pod_images = await process_file(file_bytes_value, file_name or "pod_document")
                        bb = await analyze_big_box_pod_gemini(pod_images, retailer)
                        pod_paperwork_data["trailerControlRecord"] = bb["trailerControlRecord"]
                        pod_paperwork_data["deliveryReport"] = bb["deliveryReport"]
                        pod_paperwork_data["sticker"] = bb["sticker"]
                    except Exception as e:
                        logger.exception("⚠️ Big-box POD tahlili xatosi (Load #%s)", load_display_id)
                        await send_error_to_group(f"⚠️ {retailer} qo'shimcha hujjat tahlili xatosi: {e}", group_id=chat_id)

                pod_post_result = await post_paperwork_issue(
                    pod_paperwork_data, file_bytes_value, file_name or "pod_document",
                    file_type=2, group_id=chat_id, message_id=msg.message_id,
                )
                if pod_post_result["success"]:
                    logger.info("✅ POD paperwork issue yuborildi! Load #%s", load_display_id)
                else:
                    await send_error_to_group(f"❌ POD paperwork issue xatosi: {pod_post_result.get('error')}", group_id=chat_id)

                await msg.answer(f"✅ Load #{load_display_id} POD #{pods_count}/{required_pods} received!")
                return "selected", f"POD #{pods_count}/{required_pods} received", new_load_id
            else:
                error_msg = result.get('error', 'Unknown error')
                await send_error_to_group(f"❌ POD verification error: {error_msg}", group_id=chat_id)
                await msg.answer(f"❌ POD verification failed for Load #{load_display_id}. Please try again.")
                return "skipped", f"POD verification failed: {error_msg}", new_load_id

        # file_type yuqoridagi if/elif'larga tushmadi (kutilmagan qiymat)
        return "skipped", f"Unhandled file_type={file_type}", new_load_id

    else:
        bol_data = check_result.get('bol_data', {})
        available_loads = check_result.get('available_loads', 0)
        logger.debug("📋 else block entered: bol_data=%s, available_loads=%s", bol_data, available_loads)

        pickup = bol_data.get('pickup', 'not found')
        delivery = bol_data.get('delivery', 'not found')

        if check_result.get('loadId'):
            load_id = check_result['loadId']
            await msg.answer(f"⚠️ Load #{load_id} BOL verified, but could not save to server.\nPlease try again or contact support.")
            await checking_msg.delete()
            return "skipped", "BOL verified but could not save to server", load_id

        if pickup == 'not found' and delivery == 'not found':
            logger.info("📄 Could not extract BOL data from document")
            await checking_msg.delete()
            return "skipped", "Could not extract BOL data from document", None

        logger.info("📋 Sending 'BOL did not match' message to user")
        await checking_msg.delete()
        await msg.answer("❌ BOL did not match any load.")
        return "skipped", f"BOL did not match any load (pickup={pickup}, delivery={delivery})", None
