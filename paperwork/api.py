"""POST /check-bol endpoint — orchestrates the full paperwork analysis flow."""
import asyncio
import logging
import re
import time

import fitz  # PyMuPDF — PDF text-qatlamidan bosilgan "Page X of Y" ni deterministik o'qish
from config import PO_MATCH_ENFORCE
from external import get_load_details, get_loads_from_api
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from messaging import send_error_to_group
from stats import record_paperwork_event

from .gemini import is_quota_error
from .pdf import MAX_PAGES, process_file
from .us_mail import analyze_us_mail_federal_gemini, is_us_mail_load
from .validator import validate_bol_with_loads_gemini

logger = logging.getLogger(__name__)
router = APIRouter()


def _health_field(v):
    """BOL-1: Gemini health-field'ni dict'ga majburlaydi. Gemini `null` yoki skalyar
    qaytarsa (mavjud-lekin-null holati), keyingi `["isHealthy"]`/`["summary"]` indekslash
    crash qilardi va butun paperwork issue jimgina tushib qolardi. Endi konservativ
    "tekshirilmadi" (isHealthy=False) qaytaramiz — sog'lom deb belgilamaymiz."""
    return v if isinstance(v, dict) else {"isHealthy": False, "summary": "Not checked"}


# Bosilgan sahifa-ko'rsatkichi: FAQAT "page/pg/sheet" prefiksli variantlar ("PAGE: 1 Of 3",
# "Page 1/4", "Sheet 2 of 3"). Prefiksisiz "1 of 3" ATAYIN qamrab olinmagan — hujjat matnida
# "1 of 3 pallets" kabi soxta trigger bo'lardi; ularni model o'qiydi.
_PRINTED_TOTAL_RE = re.compile(r"(?:page|pg|sheet)\s*\.?\s*:?\s*\d{1,3}\s*(?:of|/)\s*(\d{1,3})", re.I)


def _printed_total_from_pdf_text(file_bytes: bytes, file_name: str) -> int | None:
    """PDF text-qatlamidan bosilgan "Page X of Y" umumiy sonini deterministik o'qiydi.

    Model bosilgan ko'rsatkichni o'tkazib yuborsa ham (burchak/sifat), matnli PDF'larda
    bu backstop ushlaydi. Skan/rasm hujjatlarda text-qatlam bo'lmaydi → None (model javobi
    ishlatiladi). Bir nechta topilsa eng KATTASI olinadi (multi-doc paketda to'liq talab)."""
    if not (file_name or "").lower().endswith(".pdf"):
        return None
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            totals = []
            for page_num in range(min(len(doc), MAX_PAGES)):
                for m in _PRINTED_TOTAL_RE.finditer(doc.load_page(page_num).get_text()):
                    totals.append(int(m.group(1)))
            return max(totals) if totals else None
        finally:
            doc.close()
    except Exception:
        logger.debug("printed-total regex o'qishda xato — model javobi ishlatiladi", exc_info=True)
        return None


async def _attach_references(group_id: str, loads: list) -> None:
    """Har bir load'ga PO↔RC tekshiruvi uchun ikki maydon biriktiradi:
    `references` (MA'LUM raqamlar to'plami) va `poCheckMode` ("strict" | "presence").

    Manbalar: load payload'ining o'zi (loadId = RC RefNumber; backend kelajakda
    poNumber/otherNumber/references qo'shsa — avtomatik olinadi) + GET /v1/loads/{id}
    detali (QM-sync poNumber/otherNumber). Detal chaqiruvi xatosi jimgina yutiladi.

    "strict" FAQAT to'plam ISHONCHLI bo'lganda (RefNumber'dan TASHQARI kamida bitta
    raqam ma'lum) — email-RC loadlarda hujjatdagi PO aslida RC PDF ichida bo'lishi
    mumkin, lekin backend uni hali saqlamaydi; bunday holda strict qizil NOTO'G'RI
    bo'lardi → presence. US Mail ham doim presence (Route/Trip/Seal'ni us_mail
    analyzer alohida tekshiradi, USPS hujjatida RefNumber chop etilmaydi)."""

    _detail_sem = asyncio.Semaphore(8)  # backend'ga detal-so'rovlar cheklangan parallelizmda

    async def _one(ld: dict):
        refs: list = []

        def _add(v):
            s = str(v).strip() if v is not None else ""
            if s and s.lower() not in ("none", "null", "n/a") and s not in refs:
                refs.append(s)

        _add(ld.get("loadId"))
        _add(ld.get("poNumber"))
        _add(ld.get("otherNumber"))
        for r in (ld.get("references") or []):
            _add(r)
        if ld.get("id") is not None:
            async with _detail_sem:
                detail = await get_load_details(group_id, ld.get("id"))
            if not detail:
                logger.info("load %s detail olinmadi — PO tekshiruvi presence rejimda", ld.get("id"))
            _add(detail.get("poNumber"))
            _add(detail.get("otherNumber"))
            _add(detail.get("refNumber"))
        ld["references"] = refs
        ld["poCheckMode"] = "strict" if (len(refs) >= 2 and not ld.get("isUsMail")) else "presence"

    try:
        await asyncio.gather(*[_one(ld) for ld in loads])
    except Exception:
        logger.debug("reference enrichment xatosi — presence rejimga tushamiz", exc_info=True)
        for ld in loads:
            if not isinstance(ld.get("references"), list):
                ld["references"] = [str(ld["loadId"])] if ld.get("loadId") else []
            ld.setdefault("poCheckMode", "presence")


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

        # 2.4. US Mail flag — HAR DOIM hisoblanadi (HTTP'siz, arzon): weight tekshiruvi
        # rejimi (USPS BOL'da og'irlik emas, foiz bosiladi) va PO rejimi shunga qaraydi.
        for ld in loads:
            try:
                ld["isUsMail"] = bool(is_us_mail_load(ld))
            except Exception:
                ld["isUsMail"] = False

        # 2.5. PO↔RC tekshiruvi uchun har bir load'ning ma'lum referencelarini va
        # tekshiruv rejimini (strict/presence) hisoblaymiz.
        if PO_MATCH_ENFORCE:
            await _attach_references(group_id, loads)

        current_loads = [ld for ld in loads if ld.get("isCurrent") is True]
        other_loads = [ld for ld in loads if ld.get("isCurrent") is not True]
        logger.info("✅ %d current + %d other load (%d total)", len(current_loads), len(other_loads), len(loads))

        # 3. BOL/Late Slip ni avval current, keyin (kerak bo'lsa) boshqa loadlar bilan solishtirish.
        # BOL-2: matchType bo'yicha rank — kuchli "number" (reference raqam, rank 2) match
        # kuchsiz "lane" (faqat shahar, rank 1) matchni yutadi. Birinchi topilganda to'xtamay
        # eng kuchli matchni tanlaymiz; faqat kuchli number matchda erta break qilamiz.
        # Noma'lum/yo'q matchType → rank 2 (kuchli): faqat ANIQ "lane" deb belgilangan match
        # past baholanadi, boshqa hamma holat bugungi break-on-first xulqini saqlaydi.
        _MATCH_RANK = {"number": 2, "lane": 1}
        result: dict | None = None
        matched_load: dict | None = None
        best_rank = 0
        for label, subset in (("current", current_loads), ("other", other_loads)):
            if not subset:
                continue
            logger.debug("🔍 Hujjatni %s loadlar bilan Gemini orqali solishtirmoqda (%d ta)...", label, len(subset))
            res = await validate_bol_with_loads_gemini(bol_images, subset)
            if result is None:
                result = res  # doc-type / no-match report yo'llari uchun fallback
            logger.info("✅ Gemini (%s) natija: isBOL=%s, isLateSlip=%s, matchedIndex=%s, matchType=%s",
                        label, res.get('isBOL'), res.get('isLateSlip'), res.get('matchedIndex'), res.get('matchType'))

            if not res.get("isBOL", False) and not res.get("isLateSlip", False):
                # Hujjat na BOL na Late Slip — boshqa subset ham foyda bermaydi
                break

            mi = res.get("matchedIndex", 0)
            if mi and 1 <= mi <= len(subset):
                rank = _MATCH_RANK.get(str(res.get("matchType", "")).lower(), 2)
                if rank > best_rank:
                    best_rank = rank
                    matched_load = subset[mi - 1]
                    result = res  # health/pageCount/US-mail maydonlari mos kelgan call'dan olinsin
                    logger.info("✅ %s loadlar orasidan moslik topildi (rank=%d)", label, rank)
                if rank >= 2:
                    break  # kuchli number match — boshqa subsetga hojat yo'q
            else:
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
            "weight": _health_field(result.get("weight")),
            "signature": _health_field(result.get("signature")),
            "poNumber": _health_field(result.get("poNumber")),
            "pickUpAddress": _health_field(result.get("pickUpAddress")),
            "deliveryAddressAddress": _health_field(result.get("deliveryAddressAddress")),
        }

        # Pallet/piece soni: BOL'dagi son RC'dagi bilan solishtiriladi (broker kam yuk
        # uchun claim qiladi). _health_field'dan farqli default SOG'LOM (N/A) — RC'da
        # pallet ma'lumoti bo'lmagan (legacy) loadlar qizil bo'lib ketmasin.
        # isHealthy bool bo'lishi ham shart: present-but-null holatda "none" stringi
        # backend DTO'siga tushib butun POST'ni 400 qilib yuborishi mumkin edi.
        _pallet = result.get("palletCount")
        paperwork_data["palletCount"] = (
            _pallet
            if isinstance(_pallet, dict) and isinstance(_pallet.get("isHealthy"), bool)
            else {"isHealthy": True, "summary": "N/A"}
        )

        # Page count kriteriyasi — hujjatdagi bosilgan "X of Y" bilan haqiqiy sahifa
        # sonini solishtiradi. Keraksiz (truck/trailer/bo'sh) rasmlar realPages'ga
        # kirmaydi; summary'da nechta tashlangani ko'rsatiladi.
        # PWK-3: pageCount dict emas bo'lsa (Gemini "1 of 4" string qaytarsa) guard —
        # aks holda .get() AttributeError butun matched BOL'ni failure'ga aylantirardi.
        _pc = result.get("pageCount")
        if not isinstance(_pc, dict):
            _pc = {}
        _real = _pc.get("realPages")
        _printed = _pc.get("printedTotal")
        _junk = _pc.get("irrelevantPages") or 0
        # Deterministik backstop: model bosilgan ko'rsatkichni o'qimagan bo'lsa, PDF
        # text-qatlamidagi "Page X of Y" dan olamiz (skan/rasmda None — model yagona manba).
        if not isinstance(_printed, int):
            _regex_printed = _printed_total_from_pdf_text(bol_file_bytes, bol_file_name)
            if _regex_printed is not None:
                _printed = _regex_printed
                logger.info("📄 printedTotal model'dan kelmadi — PDF matnidan olindi: %s", _printed)
        pages_missing = False
        if isinstance(_real, int) and _real > 0:
            # PWK-7: hujjat MAX_PAGES'dan ko'p sahifali bo'lsa (biz cheklaganmiz) — bu
            # "incomplete" (drayver aybi) emas, balki bizning tahlil cheklovimiz.
            _capped = isinstance(_printed, int) and _printed > MAX_PAGES
            _incomplete = isinstance(_printed, int) and _real != _printed and not _capped
            # SAHIFA YETISHMAYDI: hujjatda "PAGE: 1 of 3" bosilgan, driver kamroq yuborgan.
            # Pipeline bu flag bilan BOL'ni qabul qilmay driver'dan to'liq to'plam so'raydi.
            # real > printed (ortiqcha sahifa) bloklamaydi — faqat kartada ❌.
            pages_missing = bool(_incomplete and isinstance(_printed, int) and _real < _printed)
            if _capped:
                _summary = f"{_real} of {_printed} page(s) — only first {MAX_PAGES} analyzed"
            elif isinstance(_printed, int):
                _summary = f"{_real} of {_printed} page(s)"
            else:
                _summary = f"{_real} page(s)"
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
            # isLateSlip pastda `final_late_slip` orqali bir marta yoziladi (dead write olib tashlandi).
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
            # POD bosqichi /verify-delivery'ga uzatishi uchun: loadning ma'lum
            # reference raqamlari va tekshiruv rejimi (PO↔RC tekshiruvi).
            "references": matched_load.get("references") or [],
            "poCheckMode": matched_load.get("poCheckMode") or "presence",
            # Hujjatda bosilgan sahifa soni yuborilganidan KO'P (masalan "1 of 3" dan
            # faqat 1 tasi kelgan) — pipeline BOL'ni qabul qilmay to'liq to'plam so'raydi.
            "pagesMissing": pages_missing,
            "pageCountSummary": (paperwork_data.get("pageCount") or {}).get("summary") or "",
        }

    except Exception as e:
        # Vertex kvota (429/RESOURCE_EXHAUSTED) — retry'lar ham yetmadi (askai burst
        # oynasi cho'zilgan). Bu HUJJAT xatosi emas — retryable signal qaytaramiz:
        # pipeline driver'ga "tizim band, birozdan keyin qayta yuboring" deydi,
        # log-guruhga qo'rqinchli raw 429 matni chiqmaydi.
        if is_quota_error(e):
            await record_paperwork_event(group_id, "ai_busy", latency_ms=_lat(), error="429 RESOURCE_EXHAUSTED")
            logger.warning("⏳ check-bol: Vertex kvota band (retry'lardan keyin ham) — retryable qaytarildi")
            return {"success": False, "retryable": True,
                    "message": "AI service is busy — please resend in a few minutes"}
        await record_paperwork_event(group_id, "error", latency_ms=_lat(), error=str(e)[:500])
        await send_error_to_group(f"❌ check-bol xatolik: {e}", group_id=group_id)
        logger.exception("❌ check-bol unexpected error")
        return {"success": False, "message": f"Kutilmagan xatolik: {str(e)}"}
