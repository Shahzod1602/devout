"""POST /check-bol endpoint — orchestrates the full paperwork analysis flow."""
import logging
import time

from external import get_loads_from_api
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from messaging import send_error_to_group
from stats import record_paperwork_event

from .pdf import process_file
from .us_mail import analyze_us_mail_federal_gemini, is_us_mail_load
from .validator import validate_bol_with_loads_gemini

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/check-bol")
async def check_bol_endpoint(
    group_id: str = Form(..., description="Telegram group ID (masalan: -5043574387)"),
    bol: UploadFile = File(..., description="BOL fayl (PDF yoki rasm)"),
):
    _t0 = time.time()

    def _lat():
        return int((time.time() - _t0) * 1000)

    try:
        bol_file_bytes = await bol.read()
        bol_file_name = bol.filename or "bol_document"

        # 1. BOL ni rasmlarga aylantirish
        logger.info("📄 BOL tahlil qilinmoqda...")
        bol_images = await process_file(bol_file_bytes, bol_file_name)

        # 2. API dan loadlarni olish
        logger.info("📥 Group %s uchun loadlar olinmoqda...", group_id)
        try:
            loads = await get_loads_from_api(group_id)
        except HTTPException as e:
            await record_paperwork_event(group_id, "error", latency_ms=_lat(), error=f"loads api: {e.detail}")
            return {"success": False, "message": f"Loadlarni olishda xato: {e.detail}"}

        if not loads:
            await record_paperwork_event(group_id, "no_loads", latency_ms=_lat())
            return {"success": False, "message": "Bu guruh uchun hech qanday load topilmadi",
                    "bol_data": {"pickup": "not found", "delivery": "not found"}}

        current_loads = [ld for ld in loads if ld.get("isCurrent") is True]
        other_loads = [ld for ld in loads if ld.get("isCurrent") is not True]
        logger.info("✅ %d current + %d other load (%d total)", len(current_loads), len(other_loads), len(loads))

        # 3. BOL/Late Slip ni avval current, keyin (kerak bo'lsa) boshqa loadlar bilan solishtirish
        result: dict | None = None
        matched_load: dict | None = None
        for label, subset in (("current", current_loads), ("other", other_loads)):
            if not subset:
                continue
            logger.debug("🔍 Hujjatni %s loadlar bilan Gemini orqali solishtirmoqda (%d ta)...", label, len(subset))
            result = await validate_bol_with_loads_gemini(bol_images, subset)
            logger.info("✅ Gemini (%s) natija: isBOL=%s, isLateSlip=%s, matchedIndex=%s",
                        label, result.get('isBOL'), result.get('isLateSlip'), result.get('matchedIndex'))

            if not result.get("isBOL", False) and not result.get("isLateSlip", False):
                # Hujjat na BOL na Late Slip — boshqa subset ham foyda bermaydi
                break

            mi = result.get("matchedIndex", 0)
            if mi and 1 <= mi <= len(subset):
                matched_load = subset[mi - 1]
                logger.info("✅ %s loadlar orasidan moslik topildi", label)
                break
            logger.warning("⚠️ %s loadlar orasidan moslik topilmadi, keyingisiga o'tilmoqda...", label)

        # `result` is guaranteed to be set by the loop above: `loads` is non-empty
        # (we returned early otherwise), so at least one subset is non-empty and
        # validate_bol_with_loads_gemini ran at least once.
        assert result is not None, "loop guarantees result is set"

        is_late_slip_doc = bool(result.get("isLateSlip", False))

        if not result.get("isBOL", False) and not is_late_slip_doc:
            await record_paperwork_event(group_id, "not_bol", latency_ms=_lat())
            return {"success": False, "message": "Document is not a BOL",
                    "bol_data": {"pickup": "not found", "delivery": "not found"}}

        if matched_load is None:
            await record_paperwork_event(group_id, "no_match", latency_ms=_lat())
            return {"success": False, "message": "BOL did not match any load",
                    "bol_data": {"pickup": "not found", "delivery": "not found"},
                    "available_loads": len(loads)}

        # Late Slip faqat US Mail / Federal yuklarda ruxsat etiladi
        if is_late_slip_doc and not is_us_mail_load(matched_load):
            await record_paperwork_event(group_id, "late_slip_non_usmail", latency_ms=_lat(),
                                   error=f"Late Slip on non-US-Mail load id={matched_load.get('id')}")
            return {"success": False,
                    "message": "Late Slip faqat US Mail (Federal) yuklar uchun qabul qilinadi",
                    "bol_data": {"pickup": "not found", "delivery": "not found"}}

        _raw_load_number = matched_load.get("loadId")
        _raw_load_db_id = matched_load.get("id")
        try:
            load_number = int(_raw_load_number) if _raw_load_number is not None else None
        except (ValueError, TypeError):
            load_number = _raw_load_number
        try:
            load_db_id = int(_raw_load_db_id) if _raw_load_db_id is not None else None
        except (ValueError, TypeError):
            load_db_id = _raw_load_db_id
        load_id = load_db_id if load_db_id is not None else load_number
        stops = matched_load.get("stops", [])

        logger.info("✅ Load #%s (db_id=%s) ga mos keldi", load_number, load_db_id)

        paperwork_data = {
            "loadId": load_id,
            "weight": result.get("weight", {"isHealthy": False, "summary": "Not checked"}),
            "signature": result.get("signature", {"isHealthy": False, "summary": "Not checked"}),
            "poNumber": result.get("poNumber", {"isHealthy": False, "summary": "Not checked"}),
            "pickUpAddress": result.get("pickUpAddress", {"isHealthy": False, "summary": "Not checked"}),
            "deliveryAddressAddress": result.get("deliveryAddressAddress", {"isHealthy": False, "summary": "Not checked"}),
        }

        # Page count kriteriyasi — hujjatdagi bosilgan "X of Y" bilan haqiqiy sahifa
        # sonini solishtiradi. Keraksiz (truck/trailer/bo'sh) rasmlar realPages'ga
        # kirmaydi; summary'da nechta tashlangani ko'rsatiladi.
        _pc = result.get("pageCount") or {}
        _real = _pc.get("realPages")
        _printed = _pc.get("printedTotal")
        _junk = _pc.get("irrelevantPages") or 0
        if isinstance(_real, int) and _real > 0:
            _incomplete = isinstance(_printed, int) and _real != _printed
            _summary = f"{_real} of {_printed} page(s)" if isinstance(_printed, int) else f"{_real} page(s)"
            if _junk:
                _summary += f" ({_junk} irrelevant removed)"
            paperwork_data["pageCount"] = {"isHealthy": not _incomplete, "summary": _summary}

        # US Mail / Federal load uchun qo'shimcha tahlil
        us_mail = False
        us_mail_result = None
        if is_us_mail_load(matched_load):
            logger.info("📮 Load #%s US Mail / Federal deb aniqlandi — qo'shimcha tahlil...", load_number)
            us_mail_result = await analyze_us_mail_federal_gemini(bol_images)
            us_mail = True
            logger.info("📮 US Mail tahlili: isAccepted=%s, route=%s, trip=%s, seal=%s, state=%s",
                        us_mail_result.get('isAccepted'),
                        us_mail_result.get('routeNumber', {}).get('isHealthy'),
                        us_mail_result.get('tripNumber', {}).get('isHealthy'),
                        us_mail_result.get('sealNumber', {}).get('isHealthy'),
                        us_mail_result.get('stateAddress', {}).get('isHealthy'))
            paperwork_data["isUsMail"] = True
            paperwork_data["routeNumber"] = us_mail_result.get("routeNumber") or {"isHealthy": False, "summary": "Not detected"}
            paperwork_data["tripNumber"] = us_mail_result.get("tripNumber") or {"isHealthy": False, "summary": "Not detected"}
            paperwork_data["sealNumber"] = us_mail_result.get("sealNumber") or {"isHealthy": False, "summary": "Not detected"}
            state_addr = us_mail_result.get("stateAddress") or {"isHealthy": False, "summary": "Not detected"}
            # State-only address — pickup/delivery sifatida ham US Mail uchun qaytaramiz
            paperwork_data["pickUpAddress"] = state_addr
            paperwork_data["deliveryAddressAddress"] = state_addr
            paperwork_data["isLateSlip"] = bool(us_mail_result.get("isLateSlip"))
            paperwork_data["lateSlipAllowed"] = bool(us_mail_result.get("lateSlipAllowed"))
            paperwork_data["isTeamDriverLoad"] = bool(us_mail_result.get("isTeamDriverLoad"))

        # Late Slip flag — BOL matching yoki US Mail analyzer biri ham true desa
        final_late_slip = is_late_slip_doc or (us_mail_result is not None and bool(us_mail_result.get("isLateSlip")))
        paperwork_data["isLateSlip"] = final_late_slip

        await record_paperwork_event(group_id, "matched", load_id=load_id, latency_ms=_lat())
        return {
            "success": True,
            "loadId": load_number,
            "loadDbId": load_db_id,
            "message": "Late Slip detected" if final_late_slip else "BOL verified successfully",
            "paperwork_result": paperwork_data,
            "stops": stops,
            "isUsMail": us_mail,
            "isLateSlip": final_late_slip,
        }

    except Exception as e:
        await record_paperwork_event(group_id, "error", latency_ms=_lat(), error=str(e)[:500])
        await send_error_to_group(f"❌ check-bol xatolik: {e}", group_id=group_id)
        logger.exception("❌ check-bol unexpected error")
        return {"success": False, "message": f"Kutilmagan xatolik: {str(e)}"}
