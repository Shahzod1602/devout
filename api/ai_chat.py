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

import chat_data
from config import AI_API_TOKEN, LLM_API_KEY
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from group_analyze import DEFAULT_PROMPT, analyze_companies, schedule_analyze_and_deliver
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
    # company_ids — chaqiruvchilar ba'zan son, ba'zan satr yuboradi (token
    # cache'da companyId int, JS tomonida string). Ikkalasi ham qabul qilinadi,
    # ichkarida solishtirish har doim STRING'da bo'ladi.
    company_ids: list[str | int] = Field(
        ..., min_length=1,
        description="Kompaniya ID'lari. Har birining BARCHA guruhlari avtomatik "
                    "yig'iladi va BITTA umumiy javobga birlashtiriladi (tenant "
                    "izolyatsiyasi tabiiy — boshqa kompaniya guruhi qatnashmaydi).",
    )
    prompt: str | None = Field(
        None, min_length=1,
        description="Erkin so'rov: nima tahlil qilinsin. Berilmasa — standart operatsion "
                    "xulosa prompti ishlatiladi (NEEDS ATTENTION / RESOLVED TODAY / SNAPSHOT).",
    )
    hours: int | None = Field(
        None, ge=1, le=24 * 90, description="Oxirgi N soat. Bermasangiz — butun tarix.",
    )
    ceo_id: str | int | None = Field(
        None,
        description="Telegram chat/user ID. Berilsa javob JSON'da QAYTMAYDI — "
                    "so'rov 202 bilan darhol yopiladi, tahlil fonda ishlaydi va "
                    "natija shu chatga yuboriladi.",
    )


@router.post("/analyze", dependencies=[Depends(check_ai)])
async def ai_chat_analyze(req: AnalyzeRequest):
    """Kompaniya(lar)ning BARCHA guruh xabarlarini foydalanuvchi prompti bo'yicha
    BITTA umumiy javobga tahlil qiladi (LLM gateway).

    Har `company_id` uchun `chat_data.fetch_groups` orqali guruhlar topiladi,
    ularning xabarlari birlashtirilib bitta transkriptga aylantiriladi (DB'dagi
    `chat_messages` → transkript → model → javob matni). Guruh topilmasa — o'sha
    kompaniyaga oid yozishma yo'q, xato emas (bo'sh javob).

    `prompt` ixtiyoriy — berilmasa `group_analyze.DEFAULT_PROMPT` (exception-first
    operatsion xulosa: NEEDS ATTENTION / RESOLVED TODAY / SNAPSHOT) ishlatiladi,
    ya'ni `company_ids` + `hours` yetarli — har safar matn yozish shart emas.

    **`ceo_id` berilmasa** — sinxron: javob JSON'da qaytadi, Telegram'ga hech
    nima yuborilmaydi.
    **`ceo_id` berilsa** — 202 darhol qaytadi, tahlil fonda ishlaydi va natija
    o'sha Telegram chatga yuboriladi (4096 dan uzun bo'lsa qismlarga bo'linadi).
    Fon rejimi majburiy: backend HttpClient timeouti 100s, tahlil esa undan
    uzoqroq davom etishi mumkin.
    """
    if not LLM_API_KEY:
        raise HTTPException(status_code=503, detail="LLM_API_KEY o'rnatilmagan — tahlil o'chiq")

    company_ids = [str(c) for c in req.company_ids]
    prompt = (req.prompt or "").strip() or DEFAULT_PROMPT

    if req.ceo_id is not None:
        schedule_analyze_and_deliver(
            str(req.ceo_id), company_ids=company_ids, prompt=prompt, hours=req.hours
        )
        logger.info("ai_chat_analyze: fon tahlili boshlandi (companies=%s → chat=%s)",
                    company_ids, req.ceo_id)
        return JSONResponse(
            status_code=202,
            content={"accepted": True, "company_ids": company_ids, "ceo_id": str(req.ceo_id)},
        )

    try:
        return await analyze_companies(company_ids, prompt, hours=req.hours)
    except LlmError as e:
        logger.warning("ai_chat_analyze: model xatosi (companies=%s): %s", company_ids, e)
        raise HTTPException(status_code=502, detail=f"AI javob bermadi: {e}") from None
