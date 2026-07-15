"""Control-plane auth — shared-secret `X-Api-Key` guard (audit v3 H2).

`api/routes.py` endpointlari (backend webhook'lari: /send-message, /accepted,
/permissions, /group-deleted, /group-company-changed, /update-ticket-status, ...)
avval autentifikatsiyasiz edi — tarmoq ichidagi istalgan tomon guruhlarga xabar
yuborishi yoki permissions o'zgartirishi mumkin edi.

DARK-LAUNCH: backend (C# updaterplatform, boshqa jamoa) hali `X-Api-Key`
yubormaydi. Shu sabab guard uch rejimda ishlaydi (config.py'ga qarang):

  * CONTROL_PLANE_API_KEY bo'sh    → auth O'CHIQ, hamma so'rov o'tadi (legacy).
  * key bor, ENFORCE=0 (default)   → MONITOR: kalitsiz so'rov ham O'TADI, lekin
                                     davriy adoption-summary loglanadi (backend
                                     kalit yubora boshlaganini tasdiqlash uchun).
  * key bor, ENFORCE=1             → kalitsiz yoki xato kalitli so'rov → 401.

Backend kalit yuboradigan bo'lgach ENFORCE=1 ga o'tkaziladi — kod deploy'siz,
faqat env o'zgarishi.
"""
import hmac
import logging
import time

from config import CONTROL_PLANE_API_KEY, CONTROL_PLANE_AUTH_ENFORCE, ENV_LABEL
from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_API_KEY_HEADER = "x-api-key"
_SUMMARY_INTERVAL = 300  # s — MONITOR rejimida adoption log-summary chastotasi
# Monitor holati: shu oyna ichidagi kalitli/kalitsiz so'rovlar soni + oxirgi log vaqti.
_monitor: dict = {"with_key": 0, "without_key": 0, "last_log": 0.0}

if CONTROL_PLANE_API_KEY:
    logger.info(
        "🔐 [%s] control-plane auth YOQILGAN (enforce=%s)",
        ENV_LABEL, CONTROL_PLANE_AUTH_ENFORCE,
    )


def _key_ok(supplied: str) -> bool:
    return bool(CONTROL_PLANE_API_KEY) and hmac.compare_digest(
        supplied.encode(), CONTROL_PLANE_API_KEY.encode()
    )


async def require_api_key(request: Request) -> None:
    """Router-level guard. Kalit o'rnatilmagan bo'lsa no-op; ENFORCE'da 401."""
    if not CONTROL_PLANE_API_KEY:
        return  # auth o'chiq — legacy xulq

    ok = _key_ok(request.headers.get(_API_KEY_HEADER, ""))

    if CONTROL_PLANE_AUTH_ENFORCE:
        if not ok:
            ip = request.client.host if request.client else "?"
            logger.warning(
                "🔐 control-plane 401: %s %s from %s (X-Api-Key yo'q/xato)",
                request.method, request.url.path, ip,
            )
            raise HTTPException(status_code=401, detail="Unauthorized")
        return

    # MONITOR rejimi — so'rovni O'TKAZAMIZ, lekin adoption'ni kuzatamiz.
    _monitor["with_key" if ok else "without_key"] += 1
    now = time.time()
    if now - _monitor["last_log"] >= _SUMMARY_INTERVAL:
        logger.info(
            "🔐 control-plane auth MONITOR (enforce=0, so'rovlar o'tkazildi): "
            "with_key=%d without_key=%d",
            _monitor["with_key"], _monitor["without_key"],
        )
        _monitor.update(with_key=0, without_key=0, last_log=now)
