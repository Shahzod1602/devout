"""BOL ↔ load validation: address formatting, stop counting, Gemini matcher."""
import asyncio
import json
import logging
from collections import Counter
from io import BytesIO

from PIL import Image
from stats import current_gemini_endpoint

from .gemini import gemini_extract_once

logger = logging.getLogger(__name__)


def _vote_field(samples: list[dict], key: str, default):
    """Eng ko'p uchragan qiymat (>=2 marta) — aks holda konservativ `default`."""
    try:
        value, n = Counter(s.get(key) for s in samples).most_common(1)[0]
    except TypeError:
        # PWK-6: Gemini hashlab bo'lmaydigan qiymat (masalan list) qaytarsa — Counter
        # yiqilardi. Bunda konservativ default (moslik yo'q / False) qaytaramiz.
        return default
    return value if n >= 2 else default


def _majority_vote(samples: list[dict]) -> dict:
    """3 self-consistency natijasidan isBOL/isLateSlip/matchedIndex bo'yicha ovoz beradi.

    Avval `return r3` qilinardi — bitta yuqori-varianslı 3-chi sample 2-of-3
    konsensusni ag'darib yuborardi. Endi har bir maydon bo'yicha ko'pchilik
    olinadi (matchedIndex 3 xil chiqsa → konservativ 0 = moslik yo'q). G'olib
    tuple'ga eng mos sample'ning health maydonlari ishlatiladi.
    """
    # AUD2-1: hujjat turi O'ZARO-INKOR kategoriya (hujjat ko'pi bilan BOL YOKI
    # LateSlip). isBOL/isLateSlip'ni alohida ovozga qo'yish ikkalasi ham True bo'lib
    # qolishiga olib kelardi (bitta sample invariantni buzsa → ikkita mustaqil 2/3
    # ko'pchilik), buni downstream Late Slip deb qabul qilib haqiqiy BOL'ni rad etardi.
    # Har sample'ni bitta kategoriyaga yig'ib, so'ng bir marta ovoz beramiz — both-True imkonsiz.
    def _cat(s: dict) -> str:
        bol, late = bool(s.get("isBOL")), bool(s.get("isLateSlip"))
        if bol and not late:
            return "bol"
        if late and not bol:
            return "late"
        return "neither"  # both-True (ziddiyat) yoki both-False → konservativ
    voted_cat = _vote_field([{"c": _cat(s)} for s in samples], "c", default="neither")
    voted_bol = voted_cat == "bol"
    voted_late = voted_cat == "late"
    voted_index = _vote_field(samples, "matchedIndex", default=0)
    logger.info("🗳️ Majority vote → isBOL=%s, isLateSlip=%s, matchedIndex=%s",
                voted_bol, voted_late, voted_index)

    def _score(s: dict) -> int:
        return (int(s.get("isBOL") == voted_bol)
                + int(s.get("isLateSlip") == voted_late)
                + int(s.get("matchedIndex") == voted_index))

    best = samples[0]
    for s in samples[1:]:
        if _score(s) > _score(best):
            best = s
    winner = dict(best)
    winner["isBOL"] = voted_bol
    winner["isLateSlip"] = voted_late
    winner["matchedIndex"] = voted_index
    return winner


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
  - "Bill of Lading", "BOL", "Straight Bill of Lading", "Master/House BOL", "VICS BOL"
  - "Contract Route Vehicle Record", "PS Form 5398-A", "USPS Contract Route Vehicle Record"
  - A title in ANY language for the same document (e.g. Spanish "Conocimiento de Embarque",
    "Carta de Porte", French "Connaissement") still counts if it is a signed-at-origin pickup doc
  - A signed shipping/load manifest or packing list that references a load/PO/route and is
    signed by the shipper or driver at pickup
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
  1. LOAD/BOL NUMBER (strongest): Does the document's BOL#, PO#, Pro#, Trip#, Route#, or reference number equal any load's "loadNumber"?
  2. PICKUP CITY/STATE: Does the shipper/origin/dispatch facility match a load's pickup address?
  3. DELIVERY CITY/STATE: Does the consignee/destination match a load's delivery address?
  4. WEIGHT: Use as secondary confirmation only
USPS facilities use 3-char NASS codes (e.g. 98Z, 9EZ, DEN, 07Z) plus names like P&DC / NDC /
Processing Center / Network Distribution Center — match by facility code OR city/state OR route/trip number.
Same city or metro area counts as a match even if the street address differs.

MATCH RULE (be strict — a wrong match is worse than no match):
  Set matchedIndex > 0 ONLY when EITHER
    (a) a BOL#/PO#/Pro#/Trip#/Route# on the document equals a load's loadNumber, OR
    (b) BOTH the pickup city/state AND the delivery city/state match the SAME load.
  A single city match alone is NOT sufficient — return 0.
  If two or more loads match by lane (same pickup+delivery area), you MUST use the reference
  number to disambiguate; if none disambiguates, return matchedIndex=0.
Also set "matchType" to "number" (matched via a reference number), "lane" (matched via both
cities only) or "none" (matchedIndex=0).

STEP 3 — Validate matched load fields (each isHealthy is true ONLY per the rule below):
1. WEIGHT: isHealthy=true if a numeric weight is present and readable on the document. If the
   matched load also provides a weight, note any large discrepancy in "summary" but do NOT set
   isHealthy=false on weight alone.
2. SIGNATURE: isHealthy=true ONLY if a handwritten driver/shipper signature or an inked stamp is
   visible. A printed/typed name alone, or an empty signature line, = false.
3. PO NUMBER: isHealthy=true if a BOL#/PO#/Pro#/Trip#/Route# is present and readable on the document.
4. PICKUP ADDRESS: isHealthy=true if the document's origin/shipper city AND state match the matched
   load's pickup (street differences are allowed); false if city or state differ or are unreadable.
5. DELIVERY ADDRESS: isHealthy=true if the document's destination/consignee city AND state match the
   matched load's delivery (street differences allowed); false if city or state differ or are unreadable.

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
    "matchType": "number" or "lane" or "none",
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
    logger.info("🔁 Tiebreaker result: isBOL=%s, isLateSlip=%s, matchedIndex=%s",
                r3.get('isBOL'), r3.get('isLateSlip'), r3.get('matchedIndex'))
    return _majority_vote([r1, r2, r3])
