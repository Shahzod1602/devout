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
    get_pickup_count,
    get_pods_count,
    init_load_in_cache,
    needs_more_bols,
)
from external import post_paperwork_issue, verify_delivery
from messaging import remember_paperwork_msg_link, send_error_to_group, send_paperwork_to_log_group
from paperwork import (
    analyze_big_box_pod_gemini,
    count_stops_by_type,
    determine_file_type,
    is_big_box_delivery,
    process_file,
)
from paperwork.pdf import MAX_PAGES
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


# Quick tugmalar (config.DEFAULT_QUICK_BUTTONS bilan sinxron) — ataylab bosilgan
# eskalatsiya; LLM'siz deterministik department. Model adashib "chat" desa ticket
# jimgina yo'qolib qolmasligi uchun.
_QUICK_BUTTON_DEPS = {
    "🚛 vehicle issue": "fleet",
    "📦 delivery problem": "dispatcher",
    "🔄 system issue": "updater",
    "⚠️ safety concern": "safety",
    "💰 payment issue": "accounting",
    "📋 hr question": "hr",
}


async def classify_message(text: str):
    """Muammo-darvozasi: faqat haqiqiy muammolar department'ga (=ticket), qolgani "chat".

    A/B-verified (2026-07-06, gpt-4o-mini, 149 labeled case): eski prompt oddiy
    so'rov/savol/statuslarning 68%'ini ticket qilardi; bu prompt 0% soxta ticket,
    0% o'tkazilgan muammo. "chat" javobi history API'ga yo'naltiriladi (ticket emas).
    """
    quick_dep = _QUICK_BUTTON_DEPS.get(text.strip().lower())
    if quick_dep:
        return quick_dep
    try:
        prompt = f"""
        Decide if this driver message reports a PROBLEM that a company department must act on.

        A PROBLEM means something is wrong, broken, blocked, unsafe, missing, damaged, unpaid or paid wrong, a complaint, an accident/incident, a theft — or an urgent situation where work cannot continue unless the company acts now (fuel card declined at the pump, lumper fee demanded at receiver, locked out of the app mid-delivery, driver suddenly sick or has a family emergency and cannot drive).
        Messages can be long and rambling — if ANY part of the message reports a problem, the whole message counts as a problem.
        A message saying an earlier problem happened AGAIN or is STILL not fixed is also a problem, even with no details (e.g. "same problem again", "it happened again", "still not fixed").
        A problem can be mentioned casually in the middle of unrelated talk, in any language — do not miss it.

        NOT a problem — reply "chat": greetings, small talk, thanks, jokes, acknowledgments (ok, got it), status updates (delivered, at pickup, eta ...), FYI plans, informational questions (when is payday, what's the receiver address, how do I ...), questions about how something works (how insurance coverage works, how detention pay works, vacation policy), routine requests when nothing is wrong (resend the rate con, share a phone number, add a fee to the next settlement).

        If it IS a problem (or an explicit issue report such as "Vehicle Issue", "Delivery Problem", "System Issue", "Safety Concern", "Payment Issue", "HR Question"), reply with the department:
        - "dispatcher": load, route, delivery, detention problems; driver suddenly unavailable (sick, family emergency) so the load must be re-planned
        - "fleet": vehicle or trailer mechanical problems, breakdowns, maintenance issues
        - "safety": accidents, injuries, safety hazards, violations, being pressured to drive beyond legal hours (HOS), cargo or trailer theft / break-in
        - "insurance": insurance claim or coverage problems (a general "how does insurance work / am I covered" question is NOT a problem — that is "chat")
        - "hr": employment problems (conflict, termination, contract)
        - "accounting": pay, settlement, invoice, billing, fuel card problems
        - "updater": app, bot, or system technical problems

        OVERRIDE: if the message contains "basket" or mentions any fruits or vegetables (e.g. apple, tomato, potato, onion, carrot, meva, sabzavot, olma, pomidor, kartoshka etc.), reply "updater". "Basketball" (basketbol, баскетбол) is a sport, NOT "basket" — it does not trigger this rule.

        Messages may be in English, Uzbek, or Russian.
        Reply with ONLY ONE word: chat, dispatcher, fleet, safety, insurance, hr, accounting, or updater.
        If you are not sure the message is a problem, reply "chat".

        Message: "{text}"
        """
        res = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system",
                       "content": "You triage Telegram messages from truck drivers for a logistics company. Reply with only one word from the specified categories."},
                      {"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.1,
        )
        # '"chat."' kabi punktuatsiyali javob avval valid ro'yxatdan o'tmay "updater"
        # bo'lib soxta ticket yaratardi — chetki qo'shtirnoq/nuqtani ham olamiz.
        classification = (res.choices[0].message.content or "").strip().lower().strip('".')
        valid_categories = ["chat", "dispatcher", "fleet", "safety", "insurance", "hr", "accounting", "updater"]
        if classification in valid_categories:
            return classification
        # Fail-closed: avval "updater" edi — OpenAI format-drift/outage'da HAR BIR
        # xabar ticket bo'lib to'fon qilardi. Xabar "chat" bilan history'da qoladi.
        logger.warning("⚠️ classify_message: kutilmagan javob %r — 'chat' fallback", classification)
        return "chat"
    except Exception:
        logger.exception("❌ Classify error — 'chat' fallback")
        return "chat"


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
            file_bytes_value, file_name, status, reason,
            chat_id=chat_id, load_id=load_id, message_id=msg.message_id,
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

        # Internal-team / driver paperwork-notify backend tomonidan render qilinadi
        # ("POD #<RefNumber>") va faqat issueId tugmasini oladi — asl link bo'lmaydi.
        # RefNumber == load_display_id, shuning uchun asl hujjat xabari linkini shu
        # bo'yicha eslab qolamiz; message_worker notify matniga "Open original" qo'shadi.
        remember_paperwork_msg_link(load_display_id, chat_id, msg.message_id)

        stops = check_result.get('stops', [])
        is_late_slip = bool(check_result.get('isLateSlip', False))
        file_type = determine_file_type(stops)
        logger.info("📋 determine_file_type → %s for load #%s (isLateSlip=%s)", file_type, load_display_id, is_late_slip)

        if is_late_slip:
            file_type = 4
            logger.info("📮 Late Slip aniqlandi → file_type=4 for load #%s", load_display_id)

        pickup_count, delivery_count = count_stops_by_type({"stops": stops})
        # Har yuklashda jonli pickup/delivery sonini yangilaymiz — reconsignment/qo'shilgan
        # stop bo'lsa eski #N/M qotib qolmasin. init_load_in_cache endi ON CONFLICT DO UPDATE
        # bilan faqat count ustunlarini yangilaydi (bols/pods qatorlariga tegmaydi).
        # DEEP-1: needs_more_bols pickup_count'ni shu YANGILANGAN qatordan o'qiydi, shuning
        # uchun count/init quyidagi override'dan OLDIN bajariladi.
        await init_load_in_cache(chat_id, new_load_id, pickup_count, delivery_count)

        # Agar stops'da pickup'lar complete ko'rinsa (file_type=2) lekin hali kerakli BOLlar
        # to'liq kelmagan bo'lsa — bu keyingi BOL, POD emas. DEEP-1: ilgari `not existing_bol`
        # (0 ta BOL) bilan tekshirilardi → multi-pickup yukda BOL#2 xato POD sifatida ishlanardi.
        # Endi needs_more_bols (bols_count < pickup_count) — single-pickup uchun xulq bir xil.
        if file_type == 2 and 'paperwork_result' in check_result:
            if await needs_more_bols(chat_id, new_load_id):
                logger.warning("⚠️ determine_file_type=2 but more BOLs required → treating as BOL (file_type=1)")
                file_type = 1
            else:
                logger.info("✅ all required BOLs on file → keeping file_type=2 (POD)")

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
            await msg.answer(f"✅ Load #{load_display_id} BOL #{min(bols_count, required_bols)}/{required_bols} received!")

            logger.debug("📋 check_result keys: %s", list(check_result.keys()))
            if 'paperwork_result' in check_result:
                paperwork = check_result['paperwork_result']

                # Pallet/piece mismatch — haydovchiga darhol aytamiz: u hali shipper
                # yonida, sonni qayta sanash / BOLga izoh oldirish imkoni bor
                # (broker short-load claim qilishidan OLDIN).
                _pallet_check = paperwork.get("palletCount") or {}
                if _pallet_check.get("isHealthy") is False:
                    try:
                        _pc_summary = _pallet_check.get("summary") or ""
                        await msg.answer(f"⚠️ Pallet count mismatch — {_pc_summary}".strip(" —"))
                    except Exception:
                        logger.debug("pallet mismatch ogohlantirishi yuborilmadi", exc_info=True)

                logger.debug("📋 paperwork loadId=%s, calling post_paperwork_issue...", paperwork.get('loadId'))
                bol_post_result = await post_paperwork_issue(
                    paperwork, file_bytes_value, file_name or "bol_document",
                    file_type=1, group_id=chat_id, message_id=msg.message_id,
                )
                if bol_post_result["success"]:
                    logger.info("✅ BOL paperwork issue yuborildi! Load #%s", new_load_id)
                else:
                    await send_error_to_group(f"❌ BOL paperwork issue xatosi: {bol_post_result.get('error')}", group_id=chat_id)

            return "selected", f"BOL #{min(bols_count, required_bols)}/{required_bols} received", new_load_id

        elif file_type == 2:
            await checking_msg.delete()
            bol_bytes = await get_last_bol(chat_id, new_load_id)
            if not bol_bytes:
                logger.warning("⚠️ No BOL on file for group=%s, load=%s", chat_id, new_load_id)
                return "skipped", "POD received but no BOL on file yet", new_load_id

            # PO↔RC: FAQAT strict rejimdagi loadlarda referencelarni uzatamiz —
            # presence rejimida (US Mail, faqat RefNumber ma'lum, enrichment yo'q)
            # askai'ga refs bormaydi → po_match=null → kartada N/A (eski xulq).
            _pod_refs = (
                check_result.get("references") or []
                if check_result.get("poCheckMode") == "strict"
                else []
            )
            result = await verify_delivery(bol_bytes, file_bytes_value, references=_pod_refs)
            if result.get("success"):
                verify_data = result.get("data", {})
                # AUD2-4: askai hujjatni o'qiy olmagan bo'lsa (analysis_failed) — bu POD "qabul
                # qilindi" degani EMAS. Cache'ga qo'shmay, count oshirmay, qayta yuborishni
                # so'raymiz (aks holda o'qilmagan hujjat jimgina "delivered" bo'lib qolardi).
                if bool(verify_data.get("analysis_failed")):
                    await msg.answer(f"❌ Could not read the POD for Load #{load_display_id}. Please resend a clearer copy.")
                    return "skipped", "POD analysis failed — resend requested", new_load_id

                await add_pod_to_cache(chat_id, new_load_id, msg.message_id, file_bytes_value)
                pods_count = await get_pods_count(chat_id, new_load_id)
                required_pods = await get_delivery_count(chat_id, new_load_id)

                # IC-2: `str(... or "")` — Gemini note null qaytarsa None[:200] TypeError berardi.
                address_match = bool(verify_data.get("address_match", False))
                pod_valid = bool(verify_data.get("pod_valid", False))
                address_notes = str(verify_data.get("address_notes") or "")[:200]
                pod_notes = str(verify_data.get("pod_notes") or "")[:200]

                bol_pages = verify_data.get("bol_pages")
                pod_pages = verify_data.get("pod_pages")
                # AskAI endi hujjatdagi bosilgan "X of Y" umumiy sonini (printed_total)
                # va haqiqiy sahifa sonini (bol_pages/pod_pages — keraksiz truck/trailer
                # rasmlar chiqarib tashlangan) qaytaradi.
                bol_printed = verify_data.get("bol_printed_total")
                pod_printed = verify_data.get("pod_printed_total")
                bol_junk = verify_data.get("bol_irrelevant") or 0
                pod_junk = verify_data.get("pod_irrelevant") or 0
                # Page count muammosi: BOL va POD haqiqiy sahifalari teng emas, YOKI biror hujjat
                # to'liq emas (real sahifa != bosilgan "of N"). POD-1: bosilgan total bizning
                # MAX_PAGES cheklovimizdan katta bo'lsa — bu drayver aybi emas (biz faqat birinchi
                # MAX_PAGES sahifani tahlil qildik), shuning uchun incomplete deb hisoblamaymiz.
                bol_capped = isinstance(bol_printed, int) and bol_printed > MAX_PAGES
                pod_capped = isinstance(pod_printed, int) and pod_printed > MAX_PAGES
                bol_incomplete = isinstance(bol_printed, int) and bol_pages is not None and bol_pages != bol_printed and not bol_capped
                pod_incomplete = isinstance(pod_printed, int) and pod_pages is not None and pod_pages != pod_printed and not pod_capped
                page_mismatch = (
                    bol_pages is not None and pod_pages is not None
                    and (bol_pages != pod_pages or bol_incomplete or pod_incomplete)
                )

                if page_mismatch:
                    # POD-2: page count muammosida signature/address'ni YASHIL qilib YUBORMAYMIZ.
                    # Ilgari ular majburan True (yashil "Found"/"Match") qilinardi — imzosiz yoki
                    # noto'g'ri manzilli POD yashil ko'rinib reviewer tomonidan qabul qilinardi.
                    # Endi "Not verified" (qizil) + sababi: avval page count'ni to'g'rilash kerak.
                    address_match = False
                    pod_valid = False
                    address_notes = pod_notes = "Not verified — fix page count first"

                # AskAI note "Error:" bilan boshlansa (raw xato) — do'stona note ko'rsatamiz.
                if address_notes.startswith("Error:"):
                    address_notes = "Could not analyze — please Resend."
                if pod_notes.startswith("Error:"):
                    pod_notes = "Could not analyze — please Resend."

                # PO↔RC: askai po_match bool bo'lsa — qat'iy natija (false = POD'dagi
                # PO/ref RC referencelariga mos emas). Aks holda (references
                # yuborilmagan / POD'da ref o'qilmadi / eski askai) — N/A degrade,
                # POD qabul oqimi bloklanmaydi. Page-mismatch'da tahlil ishonchsiz
                # (yuqoridagi guard bilan bir mantiq) — PO qatorini ham N/A qilamiz,
                # noto'g'ri "Not in RC" qizili bilan ticket ko'tarmaymiz.
                _po_match = verify_data.get("po_match")
                _po_notes = str(verify_data.get("po_notes") or "")[:200]
                if isinstance(_po_match, bool) and not page_mismatch:
                    _po_row = {"isHealthy": _po_match, "summary": _po_notes or ("Match" if _po_match else "Not in RC")}
                else:
                    _po_row = {"isHealthy": True, "summary": "N/A"}

                pod_paperwork_data = {
                    "loadId": new_load_id,
                    "weight": {"isHealthy": True, "summary": "N/A"},
                    "signature": {"isHealthy": pod_valid, "summary": pod_notes if pod_notes else ("Found" if pod_valid else "Not found")},
                    "poNumber": _po_row,
                    "pickUpAddress": {"isHealthy": address_match, "summary": address_notes if address_notes else ("Match" if address_match else "Mismatch")},
                    "deliveryAddressAddress": {"isHealthy": address_match, "summary": address_notes if address_notes else ("Match" if address_match else "Mismatch")},
                }
                if bol_pages is not None and pod_pages is not None:
                    bol_s = f"BOL {bol_pages}" + (f" of {bol_printed}" if isinstance(bol_printed, int) else "") + " pg"
                    pod_s = f"POD {pod_pages}" + (f" of {pod_printed}" if isinstance(pod_printed, int) else "") + " pg"
                    total_junk = bol_junk + pod_junk
                    summary = f"{bol_s}, {pod_s}" + (" — must match." if page_mismatch else "")
                    if total_junk:
                        summary += f" ({total_junk} irrelevant image(s) removed)"
                    if bol_capped or pod_capped:
                        summary += f" (only first {MAX_PAGES} pages analyzed)"
                    pod_paperwork_data["pageCount"] = {
                        "isHealthy": not page_mismatch,
                        "summary": summary,
                    }

                # Costco / Walmart / Target ga yetkazib berishda broker POD bilan birga
                # Trailer Control Record, Delivery Report va Sticker hujjatlarini talab
                # qiladi. Delivery stop shu retailerlardan biri bo'lsa — POD yuklamasini
                # Gemini bilan tahlil qilib, 3 kategoriyani paperwork issue'ga qo'shamiz.
                deliveries = [s for s in stops if (s.get("type") or "").lower() == "delivery"]
                is_big_box, retailer = is_big_box_delivery(stops)
                # BB-2: aralash multi-delivery (masalan Costco + oddiy do'kon) yukda big-box
                # qo'shimcha hujjatlarini TALAB qilmaymiz — aks holda oddiy do'kon POD'ida
                # 3 ta hujjat "topilmadi" deb soxta qizil chiqadi. Faqat (a) bitta delivery
                # va u big-box, YOKI (b) hamma delivery bir xil big-box retailer bo'lsa ishlaymiz.
                big_box_uniform = bool(
                    is_big_box and deliveries
                    and all(is_big_box_delivery([s])[1] == retailer for s in deliveries)
                )
                if is_big_box and big_box_uniform:
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
                elif is_big_box:
                    logger.info("📦 Load #%s — aralash multi-delivery, big-box qo'shimcha hujjat "
                                "tekshiruvi o'tkazib yuborildi", load_display_id)

                pod_post_result = await post_paperwork_issue(
                    pod_paperwork_data, file_bytes_value, file_name or "pod_document",
                    file_type=2, group_id=chat_id, message_id=msg.message_id,
                )
                if pod_post_result["success"]:
                    logger.info("✅ POD paperwork issue yuborildi! Load #%s", load_display_id)
                else:
                    await send_error_to_group(f"❌ POD paperwork issue xatosi: {pod_post_result.get('error')}", group_id=chat_id)

                await msg.answer(f"✅ Load #{load_display_id} POD #{min(pods_count, required_pods)}/{required_pods} received!")
                return "selected", f"POD #{min(pods_count, required_pods)}/{required_pods} received", new_load_id
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
        backend_message = check_result.get('message') or ''
        logger.debug("📋 else block entered: message=%s, bol_data=%s, available_loads=%s",
                     backend_message, bol_data, available_loads)

        pickup = bol_data.get('pickup', 'not found')
        delivery = bol_data.get('delivery', 'not found')

        if check_result.get('loadId'):
            load_id = check_result['loadId']
            await msg.answer(f"⚠️ Load #{load_id} BOL verified, but could not save to server.\nPlease try again or contact support.")
            await checking_msg.delete()
            return "skipped", backend_message or "BOL verified but could not save to server", load_id

        # Log group uchun aniq sabab — backend qaytargan haqiqiy `message`
        # (masalan: "Document is not a BOL", "BOL did not match any load",
        # "Bu guruh uchun hech qanday load topilmadi"). Avval hammasi
        # "Could not extract BOL data" ga yig'ilardi — endi aniq sabab ko'rinadi.
        reason = backend_message or "Could not extract BOL data from document"
        if available_loads:
            reason = f"{reason} ({available_loads} load(s) available)"

        if pickup == 'not found' and delivery == 'not found':
            logger.info("📄 Skipped: %s", reason)
            await checking_msg.delete()
            return "skipped", reason, None

        logger.info("📋 Sending 'BOL did not match' message to user")
        await checking_msg.delete()
        await msg.answer("❌ BOL did not match any load.")
        return "skipped", reason, None
