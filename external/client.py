"""External backend API client — login, loads, paperwork upload."""
import base64
import json
import logging
import time
from datetime import datetime

import aiohttp
import fitz  # PyMuPDF
import httpx
from config import (
    BASE_URL,
    LOGIN_CREDENTIALS,
    LOGIN_URL,
    ssl_context,
)
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Token cache (module-level singleton — was previously a global in bot.py).
_access_token: str | None = None
_token_expires_at: float = 0.0  # Unix timestamp


def _decode_token_exp(token: str) -> float:
    """JWT tokendan exp (eskirish vaqti) ni olish, imzosiz"""
    try:
        payload = token.split('.')[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = json.loads(base64.b64decode(payload))
        return float(decoded.get('exp', 0))
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        logger.debug("Failed to decode JWT exp claim from token", exc_info=True)
        return 0


def invalidate_token() -> None:
    """Force the next get_api_token() call to refetch (e.g. after 401)."""
    global _access_token, _token_expires_at
    _access_token = None
    _token_expires_at = 0.0


async def get_api_token():
    """Get a valid access token (cached, refreshed when within 5 minutes of expiry)."""
    global _access_token, _token_expires_at
    if _access_token and time.time() < _token_expires_at - 300:
        return _access_token
    _access_token = None
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.post(LOGIN_URL, json=LOGIN_CREDENTIALS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("❌ Login failed: %s %s", resp.status, text)
                    return None
                # content_type=None — prod backend Content-Type yubormaydi (#botprod-migration)
                data = await resp.json(content_type=None)
                _access_token = data.get("accessToken")
                if _access_token:
                    _token_expires_at = _decode_token_exp(_access_token)
                    if _token_expires_at == 0:
                        # JWT exp o'qib bo'lmasa, 55 daqiqa default
                        _token_expires_at = time.time() + 3300
                    logger.info("✅ Token acquired! Expires at: %s", datetime.fromtimestamp(_token_expires_at))
                return _access_token
    except Exception:
        logger.exception("❌ Token acquisition error")
        return None


async def get_loads_from_api(group_id: str) -> list:
    """Get all loads for a Telegram group from the backend.

    Raises HTTPException for *any* failure mode (timeout, connect error,
    non-2xx) so callers can pattern-match on a single exception type.
    """
    token = await get_api_token()
    if not token:
        raise HTTPException(status_code=401, detail="API login failed - token olib bo'lmadi")

    url = f"{BASE_URL}/paperwork-issues/load-ratecon/groups/{group_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Accept-Language": "EN",
        "X-Group-Id": str(group_id),
    }

    # 30s timeout matches post_paperwork_issue. Default (5s) was too tight
    # for prod backend during occasional slow periods → ConnectTimeout.
    try:
        async with httpx.AsyncClient(timeout=30) as client_http:
            response = await client_http.get(url, headers=headers)
    except httpx.TimeoutException as e:
        raise HTTPException(status_code=504, detail=f"Backend timeout: {e.__class__.__name__}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Backend network error: {e.__class__.__name__}: {e}") from e

    if response.status_code == 200:
        loads = response.json()
        if loads and len(loads) > 0:
            first_load = loads[0]
            logger.debug("📋 API dan olingan birinchi load keys: %s", list(first_load.keys()))
            logger.debug("📋 Birinchi load sample: loadId=%s, id=%s", first_load.get('loadId'), first_load.get('id'))
        return loads
    raise HTTPException(status_code=response.status_code, detail=f"API xatosi: {response.text}")


def get_pdf_page_count(file_bytes: bytes, file_name: str) -> int:
    """PDF yoki rasm fayldan sahifa sonini olish."""
    try:
        if file_name.lower().endswith('.pdf'):
            pdf_doc = fitz.open(stream=file_bytes, filetype="pdf")
            page_count = len(pdf_doc)
            pdf_doc.close()
            return page_count
        return 1
    except Exception as e:
        logger.warning("⚠️ Page count olishda xato: %s", e)
        return 1


async def post_paperwork_issue(result_data: dict, bol_file_bytes: bytes, file_name: str,
                               file_type: int = 1, group_id: int | None = None,
                               message_id: int | None = None) -> dict:
    """Upload paperwork result + file to the backend.

    file_type: 0=RateConf, 1=Bol, 2=Pod, 3=Other, 4=LateSlip
    message_id: Telegram message_id (backend file linkini qurish uchun)
    """
    token = await get_api_token()
    if not token:
        return {"success": False, "error": "Token olib bo'lmadi"}

    url = f"{BASE_URL}/v1/paperwork-issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "*/*",
        "Accept-Language": "EN",
    }
    if group_id:
        headers["X-Group-Id"] = str(group_id)

    try:
        page_count = get_pdf_page_count(bol_file_bytes, file_name)
        files = {"File": (file_name, bol_file_bytes, "application/octet-stream")}

        data = {
            "LoadId": result_data["loadId"],
            "FileType": file_type,
            "Weight.IsHealthy": str(result_data["weight"]["isHealthy"]).lower(),
            "Weight.Summary": result_data["weight"]["summary"],
            "Signature.IsHealthy": str(result_data["signature"]["isHealthy"]).lower(),
            "Signature.Summary": result_data["signature"]["summary"],
            "PoNumber.IsHealthy": str(result_data["poNumber"]["isHealthy"]).lower(),
            "PoNumber.Summary": result_data["poNumber"]["summary"],
            "PickUpAddress.IsHealthy": str(result_data["pickUpAddress"]["isHealthy"]).lower(),
            "PickUpAddress.Summary": result_data["pickUpAddress"]["summary"],
            "DeliveryAddressAddress.IsHealthy": str(result_data["deliveryAddressAddress"]["isHealthy"]).lower(),
            "DeliveryAddressAddress.Summary": result_data["deliveryAddressAddress"]["summary"],
            "PageCount.IsHealthy": "true",
            "PageCount.Summary": str(page_count),
            "RouteNumber.IsHealthy": str(result_data.get("routeNumber", {}).get("isHealthy", True)).lower(),
            "RouteNumber.Summary": result_data.get("routeNumber", {}).get("summary", "N/A"),
            "TripNumber.IsHealthy": str(result_data.get("tripNumber", {}).get("isHealthy", True)).lower(),
            "TripNumber.Summary": result_data.get("tripNumber", {}).get("summary", "N/A"),
            "SealNumber.IsHealthy": str(result_data.get("sealNumber", {}).get("isHealthy", True)).lower(),
            "SealNumber.Summary": result_data.get("sealNumber", {}).get("summary", "N/A"),
        }

        if message_id is not None:
            # Telegram deep link: supergroup id'dan -100 prefiksi olib tashlanadi
            if group_id is not None:
                gid_str = str(group_id)
                gid_clean = gid_str[4:] if gid_str.startswith("-100") else gid_str.lstrip("-")
                data["MessageId"] = f"https://t.me/c/{gid_clean}/{message_id}"
            else:
                data["MessageId"] = str(message_id)

        logger.debug("📋 Paperwork API ga yuborilayotgan data:")
        logger.debug("   URL: %s", url)
        logger.debug("   Token: %s...", token[:50])
        for key, value in data.items():
            logger.debug("   %s: %s", key, value)
        logger.debug("   File: %s (%d bytes)", file_name, len(bol_file_bytes))

        async with httpx.AsyncClient(timeout=30) as client_http:
            response = await client_http.post(url, data=data, files=files, headers=headers)
            logger.info("📨 Paperwork API response: %s", response.status_code)
            logger.debug("📨 Response body: %s", response.text[:500] if response.text else 'empty')
            if response.status_code in [200, 201]:
                return {"success": True, "response": response.json() if response.text else {}}
            return {"success": False, "error": f"Status: {response.status_code}, Response: {response.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
