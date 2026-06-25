"""BOL ↔ load validation: address formatting, stop counting, Gemini matcher."""
import asyncio
import json
import logging
from io import BytesIO

from PIL import Image
from stats import current_gemini_endpoint

from .gemini import gemini_extract_once

logger = logging.getLogger(__name__)


def format_ratecon_address(stop: dict) -> str:
    """RateCon stop'dan address stringini yaratish."""
    addr = stop.get("address", {})
    parts = []
    if addr.get("address_line_1"):
        parts.append(addr["address_line_1"])
    if addr.get("address_line_2"):
        parts.append(addr["address_line_2"])
    if addr.get("city"):
        parts.append(addr["city"])
    if addr.get("state"):
        parts.append(addr["state"])
    if addr.get("zip"):
        parts.append(addr["zip"])
    return ", ".join(parts) if parts else "not found"


def count_stops_by_type(load: dict) -> tuple:
    """Load dan pickup va delivery sonini hisoblash (minimum 1, 1)."""
    stops = load.get("stops", [])
    pickup_count = 0
    delivery_count = 0
    for stop in stops:
        stop_type = stop.get("type", "").lower()
        if stop_type == "pickup":
            pickup_count += 1
        elif stop_type == "delivery":
            delivery_count += 1
    return max(pickup_count, 1), max(delivery_count, 1)


def determine_file_type(stops: list) -> int:
    """API'dan kelgan stops asosida fayl turini aniqlash.

    1  → BOL (hali to'lmagan pickup bor)
    2  → POD (barcha pickup to'lgan, hali to'lmagan delivery bor)
    -1 → Hammasi to'lgan, hujjat kerak emas
    0  → Stops yo'q, aniqlash mumkin emas
    """
    if not stops:
        return 0
    pickups = [s for s in stops if s.get("type", "").lower() == "pickup"]
    deliveries = [s for s in stops if s.get("type", "").lower() == "delivery"]
    if any(not s.get("isComplete", False) for s in pickups):
        return 1
    if deliveries and any(not s.get("isComplete", False) for s in deliveries):
        return 2
    return -1


async def validate_bol_with_loads_gemini(bol_images: list, loads: list) -> dict:
    """BOL rasmlarni RateCon JSON bilan solishtirish (self-consistency: 2 parallel call + tiebreaker)."""
    current_gemini_endpoint.set("validate-bol-with-loads")
    loads_info = []
    for i, load in enumerate(loads):
        stops = load.get("stops", [])
        pickup_addrs = [format_ratecon_address(s) for s in stops if s.get("type") == "pickup"]
        delivery_addrs = [format_ratecon_address(s) for s in stops if s.get("type") == "delivery"]
        loads_info.append({
            "index": i + 1,
            "loadId": load.get("loadId"),
            "dbId": load.get("id"),
            "loadNumber": load.get("loadNumber"),
            "pickup": pickup_addrs,
            "delivery": delivery_addrs,
            "weight": load.get("weight", ""),
        })

    loads_json = json.dumps(loads_info, indent=2)

    prompt = f"""You are a logistics document verification expert. Carefully analyze the freight shipping document in the image.

AVAILABLE LOADS FROM SYSTEM:
{loads_json}

STEP 1 — Determine document type:
Set "isBOL" true/false AND "isLateSlip" true/false (a document is at most one of these; usually both false means it's neither).

ACCEPT as BOL (isBOL=true):
  - "Bill of Lading", "BOL", "Straight Bill of Lading"
  - "Contract Route Vehicle Record", "PS Form 5398-A", "USPS Contract Route Vehicle Record"
  - Any pickup document signed by SHIPPER or DRIVER at origin

ACCEPT as Late Slip (isLateSlip=true, isBOL=false):
  - Document titled "Late Slip", "Late-Slip", "Delay Slip", "Delay Notice", "Late Trip", "Late Arrival", "Late Departure"
  - USPS/US Mail delay/late form indicating the pickup or trip was late
  - Note the related Route#/Trip#/Load# on the document so STEP 2 can still match it to a load

REJECT as NOT BOL (isBOL=false AND isLateSlip=false):
  - Rate Confirmation / Rate Con (shows pay rates, broker/carrier info, lanes)
  - Invoice or Freight Invoice
  - Proof of Delivery / POD (signed by RECEIVER at destination)
  - Scale Ticket, Lumper Receipt, Detention form
  - Blank template with no data filled in

STEP 2 — Find matching load (set "matchedIndex" to 1-based index, or 0 if no match):
Match signals ranked by strength:
  1. LOAD/BOL NUMBER (strongest): Does the document's BOL#, PO#, Pro#, Trip#, Route#, or reference number match any load's "loadNumber"?
  2. PICKUP CITY/STATE: Does the shipper/origin/dispatch facility match a load's pickup address?
  3. DELIVERY CITY/STATE: Does the consignee/destination match a load's delivery address?
  4. WEIGHT: Use as secondary confirmation only
USPS zone codes: 98Z=Seattle WA, 9EZ=Federal Way WA, DEN=Denver CO, 07Z=New Jersey NJ, P&DC=Processing Center, NDC=Network Distribution Center
Same city or metro area counts as a match even if street address differs.
If a load's loadNumber appears anywhere on the document — that is a strong match.
Set matchedIndex=0 only if NO load matches on ANY signal.

STEP 3 — Validate matched load fields:
1. WEIGHT: Is weight present and readable? Compare with load weight if available.
2. SIGNATURE: Is there a handwritten driver/shipper signature visible?
3. PO NUMBER: Is there a BOL#/PO#/Pro#/Trip#/Route# present on the document?
4. PICKUP ADDRESS: Does document's origin/shipper match the matched load's pickup?
5. DELIVERY ADDRESS: Does document's destination/consignee match the matched load's delivery?

STEP 4 — Page count & relevance (the upload may contain several images):
  - Some images are NOT document pages: truck/trailer photos, the cab, a license plate,
    an odometer, random snapshots, or fully blank pages. These must NOT be counted as pages.
  - "printedTotal": read the document's PRINTED page indicator such as "Page 1 of 4",
    "1 of 4", "Page 1/4", "Sheet 1 of 3" and return the TOTAL N as an integer.
    If the document shows no such printed indicator, return null.
  - "realPages": how many of the provided images are GENUINE pages of THIS document.
  - "irrelevantPages": how many provided images are NOT part of the document
    (truck/trailer photos, blank pages, unrelated snapshots).

Return ONLY valid JSON — no markdown, no code fences, no extra text:
{{
    "isBOL": true or false,
    "isLateSlip": true or false,
    "matchedIndex": 0,
    "weight": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "signature": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "poNumber": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "pickUpAddress": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "deliveryAddressAddress": {{"isHealthy": true or false, "summary": "brief explanation"}},
    "pageCount": {{"printedTotal": null, "realPages": 1, "irrelevantPages": 0}}
}}"""

    # Her bir call uchun alohida PIL image ochiladi (shared stream muammosini oldini olish)
    pil_images_1 = [Image.open(BytesIO(img)) for img in bol_images]
    pil_images_2 = [Image.open(BytesIO(img)) for img in bol_images]

    # Self-consistency: 2 parallel Gemini calls
    r1, r2 = await asyncio.gather(
        gemini_extract_once(pil_images_1, prompt, 1),
        gemini_extract_once(pil_images_2, prompt, 2),
    )
    logger.info("🔁 Validate attempt 1: isBOL=%s, isLateSlip=%s, matchedIndex=%s",
                r1.get('isBOL'), r1.get('isLateSlip'), r1.get('matchedIndex'))
    logger.info("🔁 Validate attempt 2: isBOL=%s, isLateSlip=%s, matchedIndex=%s",
                r2.get('isBOL'), r2.get('isLateSlip'), r2.get('matchedIndex'))

    if (r1.get("isBOL") == r2.get("isBOL")
            and r1.get("isLateSlip") == r2.get("isLateSlip")
            and r1.get("matchedIndex") == r2.get("matchedIndex")):
        logger.info("✅ Both attempts agree")
        return r1

    logger.warning("⚠️ Disagreement between attempts, running tiebreaker (attempt 3)...")
    pil_images_3 = [Image.open(BytesIO(img)) for img in bol_images]
    r3 = await gemini_extract_once(pil_images_3, prompt, 3)
    logger.info("🔁 Tiebreaker result: isBOL=%s, matchedIndex=%s",
                r3.get('isBOL'), r3.get('matchedIndex'))
    return r3
