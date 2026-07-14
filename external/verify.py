"""External AskAI /verify-delivery client."""
import logging

import httpx
from config import VERIFY_DELIVERY_URL
from messaging import send_error_to_group

logger = logging.getLogger(__name__)


def _detect_file_info(data: bytes, base_name: str) -> tuple[str, str]:
    """Bytes magic number asosida fayl nomi va content-type aniqlash."""
    if data[:4] == b'%PDF':
        return f"{base_name}.pdf", "application/pdf"
    if data[:2] == b'\xff\xd8':
        return f"{base_name}.jpg", "image/jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return f"{base_name}.png", "image/png"
    return f"{base_name}.pdf", "application/octet-stream"


async def verify_delivery(bol_bytes: bytes, pod_bytes: bytes, references: list | None = None) -> dict:
    """BOL va POD ni /verify-delivery API ga yuborish.

    `references` — loadning ma'lum RC/sistem reference raqamlari (PO↔RC tekshiruvi
    uchun askai'ga uzatiladi; askai javobida po_match/po_notes qaytadi). Bo'sh/None
    bo'lsa yuborilmaydi — askai eski xulqda ishlaydi (po_match=null)."""
    try:
        bol_name, bol_mime = _detect_file_info(bol_bytes, "bol")
        pod_name, pod_mime = _detect_file_info(pod_bytes, "pod")
        logger.info("📤 verify_delivery: BOL=%s(%s), POD=%s(%s)", bol_name, bol_mime, pod_name, pod_mime)
        files = {
            'bol': (bol_name, bol_bytes, bol_mime),
            'pod': (pod_name, pod_bytes, pod_mime),
        }
        data = {}
        refs_str = ", ".join(str(r).strip() for r in (references or []) if str(r).strip())
        if refs_str:
            data["references"] = refs_str[:500]

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(VERIFY_DELIVERY_URL, files=files, data=data)

            if response.status_code == 200:
                result = response.json()
                logger.info("✅ Verify delivery success: %s", result)
                return {"success": True, "data": result}
            # audit v3 #4: askai 503 = AI throttle (429), hujjat xatosi EMAS — retryable
            # signal. Error-guruhga qo'rqinchli raw matn chiqarmaymiz; pipeline driver'ga
            # "tizim band, qayta yuboring" deydi.
            if response.status_code == 503:
                logger.warning("⏳ verify-delivery: askai band (503 retryable)")
                return {"success": False, "retryable": True,
                        "message": "AI service is busy — please resend in a few minutes"}
            await send_error_to_group(f"❌ Verify delivery error [{response.status_code}]: {response.text}")
            return {"success": False, "error": response.text}

    except httpx.TimeoutException as e:
        logger.error("❌ Verify delivery TIMEOUT after 180s: %s", e)
        await send_error_to_group(f"❌ Verify delivery TIMEOUT: {e}")
        return {"success": False, "error": f"Timeout: {str(e)}"}
    except Exception as e:
        await send_error_to_group(f"❌ Verify delivery exception: {e}")
        return {"success": False, "error": str(e)}
