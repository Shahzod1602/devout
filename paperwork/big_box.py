"""Big-box retailer (Costco / Walmart / Target) POD qo'shimcha hujjat tekshiruvi.

Bu retailerlarga yetkazib berishda broker POD bilan birga 3 ta qo'shimcha hujjatni
talab qiladi, aks holda to'lov qilmaydi:
  - Trailer Control Record
  - Delivery Report
  - Sticker

`us_mail.py` naqshining aynan analogi: avval load delivery stop'idan retailer
aniqlanadi (`is_big_box_delivery`), keyin POD rasmlari Gemini bilan tahlil qilinib
har bir hujjat uchun {isHealthy, summary} qaytariladi (`analyze_big_box_pod_gemini`).
"""
import logging
from io import BytesIO

from PIL import Image
from stats import current_gemini_endpoint

from .gemini import gemini_extract_once

logger = logging.getLogger(__name__)

# Har bir retailer uchun keyword variantlari → ko'rsatiladigan nom.
BIG_BOX_RETAILERS = (
    (("costco",), "Costco"),
    (("walmart", "wal-mart", "wal mart"), "Walmart"),
    (("target",), "Target"),
)


def is_big_box_delivery(stops: list) -> tuple[bool, str]:
    """DELIVERY stop manzilida Costco/Walmart/Target bormi?

    Faqat `type == "delivery"` stoplar tekshiriladi (pickup'dagi retailer trigger
    qilmaydi). Qaytaradi: (topildi_mi, retailer_nomi). Topilmasa (False, "").
    """
    for stop in stops or []:
        if (stop.get("type") or "").lower() != "delivery":
            continue
        addr = stop.get("address") or {}
        combined = " ".join([
            str(addr.get("address_line_1") or ""),
            str(addr.get("address_line_2") or ""),
            str(addr.get("locationName") or ""),
            str(stop.get("locationName") or ""),
        ]).lower()
        for keywords, name in BIG_BOX_RETAILERS:
            if any(kw in combined for kw in keywords):
                return True, name
    return False, ""


def _fallback_result(reason: str) -> dict:
    """Tahlil bo'lmaganda — 3 hujjat ham `false` (internal team qo'lda ko'rishi uchun)."""
    return {
        "trailerControlRecord": {"isHealthy": False, "summary": reason},
        "deliveryReport": {"isHealthy": False, "summary": reason},
        "sticker": {"isHealthy": False, "summary": reason},
    }


async def analyze_big_box_pod_gemini(pod_images: list, retailer: str) -> dict:
    """POD yuklamasidan 3 qo'shimcha hujjatni aniqlash (Trailer Control Record,
    Delivery Report, Sticker).

    pod_images: rasm byte'lari ro'yxati (`process_file` natijasi).
    Qaytaradi: har bir hujjat uchun {isHealthy, summary}.
    """
    current_gemini_endpoint.set("big-box-pod-analyze")

    if not pod_images:
        return _fallback_result("No POD images to analyze")

    prompt = f"""You are a freight delivery document expert. The delivery is to a {retailer} \
store, which requires THREE additional documents to be submitted together with the POD \
(Proof of Delivery), otherwise the broker will not pay.

Carefully look through ALL provided document pages and determine whether each of these \
three documents is present and readable:

1. TRAILER CONTROL RECORD — a trailer control / trailer interchange / yard control record \
(may be titled "Trailer Control Record", "Trailer Control", "Trailer Interchange", "Yard \
Check", "Gate / Trailer Log"). Shows trailer number, seal, in/out times.

2. DELIVERY REPORT — a delivery report / receiving report / unload report signed or \
stamped by the {retailer} receiver (may be titled "Delivery Report", "Receiving Report", \
"Unload Report", "Receiving Log").

3. STICKER — a {retailer} dock/door sticker, barcode label, appointment sticker, or \
receiving sticker affixed to a page (often a small printed label with a barcode, \
door/dock number, PO# or appointment number).

Return ONLY valid JSON — no markdown, no code fences, no extra text:
{{
  "trailerControlRecord": {{"isHealthy": true or false, "summary": "one short sentence"}},
  "deliveryReport": {{"isHealthy": true or false, "summary": "one short sentence"}},
  "sticker": {{"isHealthy": true or false, "summary": "one short sentence"}}
}}

Rules:
- isHealthy = true ONLY if that specific document/element is clearly present and readable \
in the provided pages.
- If a document is missing, unreadable, or you are unsure, set isHealthy = false and \
briefly say what is missing.
- Keep each summary to one short sentence."""

    try:
        pil_images = [Image.open(BytesIO(img)) for img in pod_images]
        parsed = await gemini_extract_once(pil_images, prompt, attempt_num=1)

        # Kutilgan kalitlar bo'lmasa — xavfsiz default (false).
        result = {}
        for key in ("trailerControlRecord", "deliveryReport", "sticker"):
            item = parsed.get(key)
            if isinstance(item, dict) and "isHealthy" in item:
                result[key] = {
                    "isHealthy": bool(item.get("isHealthy", False)),
                    "summary": str(item.get("summary") or ""),
                }
            else:
                result[key] = {"isHealthy": False, "summary": "Not detected"}
        logger.info("📦 %s POD qo'shimcha hujjatlar: TCR=%s, DeliveryReport=%s, Sticker=%s",
                    retailer,
                    result["trailerControlRecord"]["isHealthy"],
                    result["deliveryReport"]["isHealthy"],
                    result["sticker"]["isHealthy"])
        return result
    except Exception as e:
        logger.exception("⚠️ analyze_big_box_pod_gemini failed")
        return _fallback_result(f"Validation error: {str(e)}")
