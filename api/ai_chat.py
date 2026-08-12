"""Tashqi AI agentlar uchun chat-log API — faqat o'qish.

Auth — `AI_API_TOKEN` (ADMIN_TOKEN'dan MUSTAQIL, alohida env). Agent
`X-API-Key` header bilan kiradi; admin panelning boshqa hech qanday
qismiga (DB brauzer, log'lar, state) kirish yo'q.

`api_key_header` FastAPI Security sifatida e'lon qilingan — shu tufayli
/docs (Swagger UI) sahifasida "Authorize" tugmasi chiqadi: tokenni bir
marta kiritib qo'ysangiz, "Try it out" har bir so'rovga avtomatik qo'shadi.

Ma'lumot manbai `chat_data.py` — xuddi shu funksiyalarni `api/chatlog.py`
(admin UI, cookie-auth) ham ishlatadi.
"""
import hmac
import logging
from typing import Any

import chat_data
from config import AI_API_TOKEN, LLM_API_KEY
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from group_analyze import analyze_group, schedule_analyze_and_deliver
from llm_gateway import LlmError
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai/chat")

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def check_ai(api_key: str | None = Depends(_api_key_header)) -> None:
    if not AI_API_TOKEN:
        raise HTTPException(status_code=503, detail="AI_API_TOKEN o'rnatilmagan — endpoint o'chiq")
    if not api_key or not hmac.compare_digest(api_key.encode(), AI_API_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.get("/groups", dependencies=[Depends(check_ai)])
async def ai_chat_groups(company_id: str | None = None):
    """Guruhlar — nomi, oxirgi xabar, kompaniya.

    `company_id` berilsa faqat o'sha kompaniya guruhlari qaytadi (kompaniyasi
    noma'lum guruhlar ro'yxatga TUSHMAYDI).
    """
    return {"groups": await chat_data.fetch_groups(company_id)}


@router.get("/messages", dependencies=[Depends(check_ai)])
async def ai_chat_messages(group_id: str, before_id: int = 0, limit: int = 50):
    """Bitta guruh xabar tarixi, eskidan-yangiga. `before_id` bilan orqaga sahifalanadi."""
    items, has_more = await chat_data.fetch_messages(group_id, before_id, limit)
    return {"items": items, "has_more": has_more}


@router.get("/file/{msg_pk_id}", dependencies=[Depends(check_ai)])
async def ai_chat_file(msg_pk_id: int):
    """Rasm/fayl/ovoz/video — Telegram'dan real vaqtda proksi qilinadi."""
    return await chat_data.proxy_file(msg_pk_id)


class AnalyzeRequest(BaseModel):
    # group_id/company_id — chaqiruvchilar ba'zan son, ba'zan satr yuboradi
    # (token cache'da companyId int, JS tomonida string). Ikkalasi ham qabul
    # qilinadi va solishtirish har doim STRING'da bo'ladi.
    group_id: str | int = Field(..., description="Telegram guruh ID (masalan '-1002345678')")
    company_id: str | int | None = Field(
        None,
        description="Berilsa — guruh AYNAN shu kompaniyaniki ekani tekshiriladi, "
                    "aks holda 403 (tenant izolyatsiyasi).",
    )
    prompt: str = Field(..., min_length=1, description="Erkin so'rov: nima tahlil qilinsin")
    hours: int | None = Field(None, ge=1, le=24 * 90, description="Oxirgi N soat (since'siz)")
    since: str | None = Field(None, description="ISO sana/vaqt (UTC), masalan 2026-08-10T00:00:00")
    until: str | None = Field(None, description="ISO sana/vaqt (UTC)")
    max_messages: int | None = Field(None, ge=1, le=5000)
    model: str | None = Field(None, description="Model override (default LLM_MODEL)")
    ceo_id: str | int | None = Field(
        None,
        description="Telegram chat/user ID. Berilsa javob JSON'da QAYTMAYDI — "
                    "so'rov 202 bilan darhol yopiladi, tahlil fonda ishlaydi va "
                    "natija shu chatga yuboriladi.",
    )


@router.post("/analyze", dependencies=[Depends(check_ai)])
async def ai_chat_analyze(req: AnalyzeRequest):
    """Guruh xabarlarini foydalanuvchi prompti bo'yicha tahlil qiladi (LLM gateway).

    DB'dagi `chat_messages` → transkript → model → javob matni.
    `since` va `hours` ikkalasi berilsa `since` ustun.
    `company_id` berilsa — guruh o'sha kompaniyaniki ekani tekshiriladi (403).

    **`ceo_id` berilmasa** — sinxron: javob JSON'da qaytadi, Telegram'ga hech
    nima yuborilmaydi.
    **`ceo_id` berilsa** — 202 darhol qaytadi, tahlil fonda ishlaydi va natija
    o'sha Telegram chatga yuboriladi (4096 dan uzun bo'lsa qismlarga bo'linadi).
    Fon rejimi majburiy: backend HttpClient timeouti 100s, tahlil esa undan
    uzoqroq davom etishi mumkin.
    """
    if not LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY o'rnatilmagan — tahlil o'chiq")

    group_id = str(req.group_id)

    if req.company_id is not None:
        # Tenant darvozasi: noto'g'ri filtr = bir kompaniya boshqasining yozishmasini
        # ko'radi. Egaligi ISBOTLANMAGAN guruh (token cache'da yo'q) ham rad etiladi.
        owner = chat_data.group_company_id(group_id)
        if owner is None or owner != str(req.company_id):
            logger.warning(
                "ai_chat_analyze: company mos kelmadi (group=%s, so'ralgan=%s, haqiqiy=%s)",
                group_id, req.company_id, owner,
            )
            raise HTTPException(status_code=403, detail="Guruh bu kompaniyaga tegishli emas")

    window: dict[str, Any] = {
        "hours": req.hours, "since": req.since, "until": req.until,
        "max_messages": req.max_messages, "model": req.model,
    }

    if req.ceo_id is not None:
        schedule_analyze_and_deliver(
            str(req.ceo_id), group_id=group_id, prompt=req.prompt, **window
        )
        logger.info("ai_chat_analyze: fon tahlili boshlandi (group=%s → chat=%s)",
                    group_id, req.ceo_id)
        return JSONResponse(
            status_code=202,
            content={"accepted": True, "group_id": group_id, "ceo_id": str(req.ceo_id)},
        )

    try:
        return await analyze_group(group_id, req.prompt, **window)
    except LlmError as e:
        logger.warning("ai_chat_analyze: model xatosi (group=%s): %s", group_id, e)
        raise HTTPException(status_code=502, detail=f"AI javob bermadi: {e}") from None
