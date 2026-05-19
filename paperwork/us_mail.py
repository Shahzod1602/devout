"""US Mail / Federal load detection and specialized Gemini analyzer."""
import logging
from io import BytesIO

from PIL import Image
from stats import current_gemini_endpoint

from .gemini import gemini_extract_once

logger = logging.getLogger(__name__)

USMAIL_BROKER_KEYWORDS = ("usps", "us mail", "u.s. mail", "u.s.p.s", "united states postal")
USMAIL_COMMODITY_KEYWORDS = ("usps", "us mail", "u.s. mail", "federal mail", "mail")
USMAIL_STOP_PATTERNS = ("USPS", "P&DC", "NDC", "PROCESSING CENTER", "NETWORK DISTRIBUTION", "U.S. POSTAL")


def is_us_mail_load(load: dict) -> bool:
    """Load US Mail / Federal load ekanligini broker, commodity yoki stops patternlaridan aniqlash."""
    if not load:
        return False
    broker = (load.get("broker") or "").lower()
    if any(k in broker for k in USMAIL_BROKER_KEYWORDS):
        return True
    commodity = (load.get("commodity") or "").lower()
    if any(k in commodity for k in USMAIL_COMMODITY_KEYWORDS):
        return True
    for stop in load.get("stops", []) or []:
        addr = stop.get("address") or {}
        combined = " ".join([
            str(addr.get("address_line_1") or ""),
            str(addr.get("address_line_2") or ""),
            str(addr.get("locationName") or ""),
            str(stop.get("locationName") or ""),
        ]).upper()
        if any(p in combined for p in USMAIL_STOP_PATTERNS):
            return True
    return False


async def analyze_us_mail_federal_gemini(bol_images: list) -> dict:
    """US Mail (Federal) hujjatni tahlil qilish: route #, trip #, state, seal #."""
    current_gemini_endpoint.set("us-mail-federal-analyze")
    prompt = """You are a transportation document expert for USPS/US Mail federal loads.

Analyze the provided document images and return EXACT JSON only (no markdown, no extra text):
{
  "isUsMailFederalLoad": true/false,
  "isLateSlip": true/false,
  "isTeamDriverLoad": true/false,
  "routeNumber": {"isHealthy": true/false, "value": "string", "summary": "one short sentence"},
  "tripNumber": {"isHealthy": true/false, "value": "string", "summary": "one short sentence"},
  "stateAddress": {"isHealthy": true/false, "value": "2-letter state or full state name", "summary": "one short sentence"},
  "sealNumber": {"isHealthy": true/false, "value": "string", "summary": "one short sentence"},
  "lateSlipAllowed": true/false,
  "isAccepted": true/false,
  "notes": "one short sentence"
}

Rules:
- isUsMailFederalLoad = true if document clearly indicates USPS/US Mail/Federal load context (USPS, U.S. Mail, Contract Route, PS Form 5398-A, federal mail route terms).
- isLateSlip = true only if this document is a Late Slip / delay slip.
- isTeamDriverLoad = true only if document indicates team/co-driver context (team driver, co-driver, driver 1/2, two driver fields, equivalent evidence).
- stateAddress.isHealthy = true only when at least one valid US state is found (2-letter code like CA/TX or full state name). For US Mail loads, state-only match is sufficient — city/street do not need to match.
- lateSlipAllowed = true only when isLateSlip=true AND isUsMailFederalLoad=true AND isTeamDriverLoad=true.
- isAccepted = true only when ALL are true: routeNumber.isHealthy, tripNumber.isHealthy, stateAddress.isHealthy, sealNumber.isHealthy, isUsMailFederalLoad.
- If evidence is unclear/missing, prefer false and explain briefly.
"""

    try:
        pil_images = [Image.open(BytesIO(img)) for img in bol_images]
        parsed = await gemini_extract_once(pil_images, prompt, attempt_num=1)

        route_ok = bool(parsed.get("routeNumber", {}).get("isHealthy", False))
        trip_ok = bool(parsed.get("tripNumber", {}).get("isHealthy", False))
        state_ok = bool(parsed.get("stateAddress", {}).get("isHealthy", False))
        seal_ok = bool(parsed.get("sealNumber", {}).get("isHealthy", False))
        federal_ok = bool(parsed.get("isUsMailFederalLoad", False))
        team_ok = bool(parsed.get("isTeamDriverLoad", False))
        late_slip = bool(parsed.get("isLateSlip", False))

        parsed["lateSlipAllowed"] = bool(late_slip and federal_ok and team_ok)
        parsed["isAccepted"] = bool(route_ok and trip_ok and state_ok and seal_ok and federal_ok)
        return parsed
    except Exception as e:
        logger.exception("⚠️ analyze_us_mail_federal_gemini failed")
        return {
            "isUsMailFederalLoad": False,
            "isLateSlip": False,
            "isTeamDriverLoad": False,
            "routeNumber": {"isHealthy": False, "value": "", "summary": "Could not validate"},
            "tripNumber": {"isHealthy": False, "value": "", "summary": "Could not validate"},
            "stateAddress": {"isHealthy": False, "value": "", "summary": "Could not validate"},
            "sealNumber": {"isHealthy": False, "value": "", "summary": "Could not validate"},
            "lateSlipAllowed": False,
            "isAccepted": False,
            "notes": f"Validation error: {str(e)}",
        }
