"""Gemini SDK setup + low-level call wrapper for paperwork analysis."""
import asyncio
import functools
import json
import logging
import os
import random
import time
from io import BytesIO

from config import (
    GEMINI_BOT_MODEL,
    GEMINI_FALLBACK_ENABLE,
    GEMINI_FALLBACK_LOCATION,
    GEMINI_FALLBACK_MODEL,
    GEMINI_TEXT_MODEL,
    VERTEX_LOCATION,
    VERTEX_PROJECT,
)
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from stats import record_gemini_call

logger = logging.getLogger(__name__)

# Vertex 429 / RESOURCE_EXHAUSTED (kvota) uchun exponential-backoff retry.
# Google'ning o'z tavsiyasi: 429 da "try again later" — shuning uchun darrov
# yiqitmasdan, jitter bilan bir necha marta qayta urinamiz. Burst paytida
# parallel call'lar jitter tufayli yoyilib, daqiqalik limitga bosimni kamaytiradi.
# 2026-07-10: askai ratecon endi 96-parallel burst qiladi (o'sha Vertex loyihasi) —
# bot'ning retry oynasi burst oynasidan (~15-30s) UZUNROQ bo'lishi shart, aks holda
# barcha urinishlar burst ichiga tushib baribir yiqiladi. 6 urinish, cap 15s:
# kutishlar 2,4,8,15,15 (+jitter) ≈ 44-49s qamrov (pipeline'ning 180s timeout'iga sig'adi).
_QUOTA_MAX_RETRIES = 6          # jami urinishlar (1 asosiy + 5 retry)
_QUOTA_BACKOFF_BASE = 2.0       # 2s, 4s, 8s, 15s, 15s (cap) + 0..1s jitter
_QUOTA_BACKOFF_CAP = 15.0

# Paperwork vision thinking byudjeti: 0 = O'CHIQ (default). Dinamik thinking (3.5-flash davri)
# kuniga ~600-700k YASHIRIN token (~$6, stats'da total-in-out delta) yer edi; askai POD A/B
# (real PROD hujjatlar) — verdictlar thinking'siz mos (59/60), ratecon precedenti bilan bir xil.
# Eski dinamik xulq (kill-switch): PAPERWORK_THINKING_BUDGET=-1.
_PW_THINK_RAW = os.getenv("PAPERWORK_THINKING_BUDGET", "0").strip()
PW_THINKING_CONFIG = (None if _PW_THINK_RAW == "-1"
                      else genai_types.ThinkingConfig(thinking_budget=int(_PW_THINK_RAW)))

# Vertex AI (service account) orqali — API key emas, shuning uchun key muddati
# tugashi muammosi yo'q. Credential `GOOGLE_APPLICATION_CREDENTIALS` orqali.
# Lazy: client faqat birinchi haqiqiy chaqiruvda yaratiladi. Aks holda
# credential'siz muhitda (CI/test) import paytida `DefaultCredentialsError`
# bilan crash bo'lardi.
_genai_client: genai.Client | None = None


def get_genai_client() -> genai.Client:
    """Vertex AI Gemini client'ini lazy ravishda yaratib qaytaradi."""
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT,
            location=VERTEX_LOCATION,
        )
    return _genai_client


# Zaxira client — boshqa region (global emas), faqat fallback yo'lida ishlatiladi.
_fallback_client: genai.Client | None = None


def get_fallback_client() -> genai.Client:
    """Fallback region uchun Vertex client (lazy)."""
    global _fallback_client
    if _fallback_client is None:
        _fallback_client = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT,
            location=GEMINI_FALLBACK_LOCATION,
        )
    return _fallback_client


def parse_gemini_json(raw: str) -> dict:
    """Gemini javobidan JSON ajratib olish (markdown fences, prefix/suffix tozalanadi)."""
    raw = raw.strip()
    if '```json' in raw:
        raw = raw.split('```json')[1].split('```')[0].strip()
    elif '```' in raw:
        raw = raw.split('```')[1].split('```')[0].strip()
    if not raw.startswith('{'):
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start != -1 and end > start:
            raw = raw[start:end]
    return json.loads(raw)


def is_quota_error(exc: Exception) -> bool:
    """429 / RESOURCE_EXHAUSTED (Vertex kvota) xatosini aniqlash."""
    if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 429:
        return True
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


_is_quota_error = is_quota_error  # ichki nom saqlanadi (mavjud chaqiruvlar uchun)


def is_transient_server_error(exc: Exception) -> bool:
    """Vertex 5xx (502/503/504) — transient server/endpoint xatosi, retry'ga loyiq.

    2026-07-18: global endpoint 502 Bad Gateway (HTML sahifa bilan) BOL-check'ni
    yiqitgan edi — SDK'ning ichki tenacity retry'i ham yetmagan. Endi 429 bilan
    bir xil yo'l: backoff + 2 urinishdan keyin regional zaxira.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code >= 500:
        return True
    text = str(exc)
    return "Bad Gateway" in text or "Service Unavailable" in text or "UNAVAILABLE" in text


def _is_retryable_error(exc: Exception) -> bool:
    """429 (kvota/DSQ) YOKI 5xx — ikkalasida ham backoff+fallback ishlaydi."""
    return is_quota_error(exc) or is_transient_server_error(exc)


async def _generate_with_backoff(text_prompt: str, image_parts: list,
                                 thinking_config=PW_THINKING_CONFIG):
    """generate_content chaqiruvi — 429/RESOURCE_EXHAUSTED bo'lsa backoff bilan retry.

    `thinking_config` default PW_THINKING_CONFIG (thinking=0). Rescue-pass kabi
    alohida chaqiruvlar o'z konfigini beradi (None = dinamik thinking).

    Har bir urinish (muvaffaqiyatli yoki yo'q) `record_gemini_call` orqali yoziladi.
    Kvota bilan bog'liq bo'lmagan xatolar darrov qayta tashlanadi (retry qilinmaydi).

    2026-07-17 fallback: asosiy model 2 marta ketma-ket 429 bersa (Google global-endpoint
    sig'im inqirozi — kvota emas), qolgan urinishlar zaxira model/regionda davom etadi.
    Holat yopishqoq EMAS: har yangi so'rov yana asosiy modeldan boshlaydi.
    """
    loop = asyncio.get_event_loop()
    use_fallback = False
    for quota_retry in range(_QUOTA_MAX_RETRIES):
        client = get_fallback_client() if use_fallback else get_genai_client()
        model = GEMINI_FALLBACK_MODEL if use_fallback else GEMINI_BOT_MODEL
        call_start = time.time()
        response = None
        try:
            response = await loop.run_in_executor(
                None,
                functools.partial(
                    client.models.generate_content,
                    model=model,
                    # genai-sdk `contents` expects invariant list; mixed str+Part is OK at runtime
                    contents=[text_prompt, *image_parts],
                    # temperature=0.2: BOL self-consistency avval Vertex default (~1.0) da
                    # ishlardi — shu sabab r1/r2 tez-tez kelishmay 3-chi tiebreaker yonardi.
                    # Past harorat kelishmovchilikni kamaytiradi (kamroq pullik 3-chi call,
                    # barqarorroq health/match maydonlari), lekin biroz diversity qoladi.
                    config=genai_types.GenerateContentConfig(max_output_tokens=10000, temperature=0.2,
                                                             thinking_config=thinking_config),
                ),
            )
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(model, response, latency_ms, success=True)
            return response
        except Exception as exc:
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(model, response, latency_ms, success=False)
            if _is_retryable_error(exc):
                if not use_fallback and GEMINI_FALLBACK_ENABLE and quota_retry >= 1:
                    # 2 urinish ham throttle/5xx — zaxiraga darhol (sleep'siz) o'tamiz.
                    use_fallback = True
                    logger.warning(
                        "🔀 Gemini FALLBACK: %s throttled/5xx → %s@%s (urinish %d/%d)",
                        GEMINI_BOT_MODEL, GEMINI_FALLBACK_MODEL, GEMINI_FALLBACK_LOCATION,
                        quota_retry + 1, _QUOTA_MAX_RETRIES,
                    )
                    continue
                if quota_retry < _QUOTA_MAX_RETRIES - 1:
                    delay = min(_QUOTA_BACKOFF_BASE * (2 ** quota_retry), _QUOTA_BACKOFF_CAP) + random.uniform(0, 1)
                    logger.warning(
                        "⏳ Gemini 429/5xx — backoff %.1fs (urinish %d/%d)",
                        delay, quota_retry + 1, _QUOTA_MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
            raise
    # range tugashi mumkin emas (oxirgi urinishda raise bo'ladi), faqat tip uchun:
    raise RuntimeError("Gemini quota backoff retries exhausted")


# Text-yo'lak (classify/summarize/priority/voice) 429-himoyasi — rasm-yo'lakdagi
# _generate_with_backoff naqshining chat-oqimiga mos QISQA varianti. classify'ni
# message-handler await qiladi, shuning uchun 45s oyna mumkin emas: yakka DSQ
# throttle'lar soniyalarda o'tadi, ommaviy inqirozda esa zaxira region baribir
# darhol javob beradi — uzoq kutishning foydasi yo'q.
_TEXT_MAX_RETRIES = 4      # 2 asosiy (GEMINI_TEXT_MODEL@global) + 2 zaxira (regional)
_TEXT_BACKOFF_BASE = 1.0   # 1s, 2s (cap) + 0..0.5s jitter — worst-case ~3.5s kutish
_TEXT_BACKOFF_CAP = 2.0


async def _text_generate_with_fallback(
    contents, config: genai_types.GenerateContentConfig, *, primary_model: str | None = None,
) -> str:
    """GEMINI_TEXT_MODEL (yoki `primary_model` berilsa o'sha) chaqiruvi — 429'da qisqa
    backoff, 2 ketma-ket throttle'dan keyin GEMINI_FALLBACK_MODEL@GEMINI_FALLBACK_LOCATION
    bilan davom etadi.

    Rasm-yo'lak bilan bir xil semantika: yopishqoq EMAS (har yangi so'rov asosiy
    modeldan boshlaydi), har urinish record_gemini_call'ga HAQIQIY model bilan
    yoziladi, kvota bo'lmagan xato DARROV raise — chaqiruvchilarning fail-closed
    default'lari (classify→"chat", priority→"medium", summarize→original) o'z kuchida.
    """
    loop = asyncio.get_event_loop()
    use_fallback = False
    # Zaxira (3.5-flash) — thinking-model: kichik max_output_tokens'da (classify=20)
    # butun budjetni thinking yeb, matn BO'SH qaytadi (empirik: 3/3 bo'sh; budget=0
    # bilan 3/3 'pong'). Text-vazifalar thinking talab qilmaydi — o'chirib yuboramiz.
    fallback_config = config.model_copy(
        update={"thinking_config": genai_types.ThinkingConfig(thinking_budget=0)},
    )
    for attempt in range(_TEXT_MAX_RETRIES):
        client = get_fallback_client() if use_fallback else get_genai_client()
        model = GEMINI_FALLBACK_MODEL if use_fallback else (primary_model or GEMINI_TEXT_MODEL)
        call_start = time.time()
        response = None
        try:
            response = await loop.run_in_executor(
                None,
                functools.partial(
                    client.models.generate_content,
                    model=model,
                    contents=contents,
                    config=fallback_config if use_fallback else config,
                ),
            )
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(model, response, latency_ms, success=True)
            return (response.text or "").strip()
        except Exception as exc:
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(model, response, latency_ms, success=False)
            if _is_retryable_error(exc) and attempt < _TEXT_MAX_RETRIES - 1:
                if not use_fallback and GEMINI_FALLBACK_ENABLE and attempt >= 1:
                    # 2 urinish ham throttle/5xx — zaxiraga sleep'siz o'tamiz.
                    use_fallback = True
                    logger.warning(
                        "🔀 Gemini TEXT fallback: %s throttled/5xx → %s@%s (urinish %d/%d)",
                        GEMINI_TEXT_MODEL, GEMINI_FALLBACK_MODEL, GEMINI_FALLBACK_LOCATION,
                        attempt + 1, _TEXT_MAX_RETRIES,
                    )
                    continue
                delay = min(_TEXT_BACKOFF_BASE * (2 ** attempt), _TEXT_BACKOFF_CAP) + random.uniform(0, 0.5)
                logger.warning(
                    "⏳ Gemini TEXT 429/5xx — backoff %.1fs (urinish %d/%d, model=%s)",
                    delay, attempt + 1, _TEXT_MAX_RETRIES, model,
                )
                await asyncio.sleep(delay)
                continue
            raise
    raise RuntimeError("Gemini text quota retries exhausted")  # yetib kelmaydi (oxirgisi raise qiladi)


async def gemini_text_completion(
    system: str,
    prompt: str,
    *,
    max_tokens: int = 60,
    temperature: float = 0.1,
    model: str | None = None,
    thinking_budget: int | None = None,
) -> str:
    """Matnli (rasm'siz) Gemini chaqiruvi — sobiq gpt-4o-mini o'rnida
    (classify_message / summarize_text / detect_priority / group_analyze.llm_chat).

    2026-07-17: OpenAI hisobida insufficient_quota (kredit tugagan) — GEMINI_TEXT_MODEL
    (flash-lite) ga ko'chirildi. 429'da _text_generate_with_fallback qisqa retry +
    regional zaxira qiladi; boshqa xatoda RAISE — chaqiruvchilarning mavjud
    fail-closed try/except'lari o'z default'iga tushadi.

    `model` berilsa GEMINI_TEXT_MODEL o'rniga o'sha ishlatiladi (429/5xx'da baribir
    GEMINI_FALLBACK_MODEL'ga o'tadi — zaxira umumiy). `thinking_budget` berilsa
    (masalan 0) asosiy chaqiruvga ham qo'yiladi — flash-lite'dan boshqa (thinking-model)
    override berilganda kichik max_output_tokens butun budjetni fikrlashga
    yeb, bo'sh javob qaytarmasin (memory: paperwork/fallback'dagi bir xil dars).
    """
    config = genai_types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        temperature=temperature,
        system_instruction=system,
    )
    if thinking_budget is not None:
        config = config.model_copy(
            update={"thinking_config": genai_types.ThinkingConfig(thinking_budget=thinking_budget)},
        )
    return await _text_generate_with_fallback(prompt, config, primary_model=model)


async def gemini_transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Ovozli xabar transkripsiyasi — sobiq OpenAI whisper-1 o'rnida (2026-07-17,
    OpenAI insufficient_quota). GEMINI_TEXT_MODEL audio-input'ni qo'llaydi (jonli
    tekshirilgan: flash-lite audio/wav sinusga to'g'ri NO_SPEECH dedi). 429'da
    _text_generate_with_fallback retry+zaxira qiladi (3.5-flash ham multimodal —
    audio qo'llaydi); boshqa xatoda RAISE — chaqiruvchi (handle_voice) o'zi ushlab
    error-guruhga yozadi.
    """
    # genai-sdk `contents` invariant-list kutadi — mavjud idiom: bare list (mypy uchun).
    contents_parts: list = [
        genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        genai_types.Part.from_text(text=(
            "Transcribe this voice message verbatim. Reply with ONLY the "
            "transcribed text, no commentary. Keep the original language "
            "(English, Uzbek, or Russian). If there is no speech, reply "
            "with an empty string."
        )),
    ]
    return await _text_generate_with_fallback(
        contents_parts,
        genai_types.GenerateContentConfig(max_output_tokens=1000, temperature=0.0),
    )


async def gemini_extract_once(pil_images: list, text_prompt: str, attempt_num: int,
                              thinking_budget: int | None = None) -> dict:
    """Gemini'dan bir marta ma'lumot olish (cost + latency tracking bilan).

    JSON parse xatosi bo'lsa 3 marta retry qilinadi. API chaqiruvi 429/
    RESOURCE_EXHAUSTED (kvota) bilan yiqilsa, `_generate_with_backoff` ichida
    exponential backoff bilan qayta urinadi. Har bir urinish record_gemini_call
    (success=True/False) orqali yoziladi.

    `thinking_budget`: None → global default (PW_THINKING_CONFIG, thinking=0);
    -1 → dinamik thinking (model o'zi hal qiladi); N → aynan shu byudjet.
    Rescue-pass (no_match'dan keyingi chuqur urinish) shu knob bilan chaqiradi.
    """
    if thinking_budget is None:
        thinking_config = PW_THINKING_CONFIG
    elif thinking_budget == -1:
        thinking_config = None  # dinamik — SDK'ga thinking_config yuborilmaydi
    else:
        thinking_config = genai_types.ThinkingConfig(thinking_budget=thinking_budget)

    image_parts = []
    for img in pil_images:
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_parts.append(genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

    for retry in range(3):
        response = await _generate_with_backoff(text_prompt, image_parts, thinking_config)
        # PWK-8: safety-block/bo'sh candidate holatida response.text ValueError tashlashi
        # mumkin — guard qilamiz, aks holda self-consistency ichida kutilmagan crash.
        try:
            result = (response.text or "").strip()
        except (ValueError, AttributeError):
            result = ""
        if not result:
            logger.warning("⚠️ Gemini bo'sh javob qaytardi (attempt %d, retry %d) — "
                           "truncation/safety-block bo'lishi mumkin", attempt_num, retry + 1)
        logger.debug("🤖 Gemini attempt %d raw response: %s", attempt_num, result[:500])
        try:
            return parse_gemini_json(result)
        except json.JSONDecodeError as e:
            logger.warning("⚠️ Gemini JSON parse error (attempt %d, retry %d): %s | raw len=%d",
                           attempt_num, retry + 1, e, len(result))
            if retry == 2:
                raise
            await asyncio.sleep(1)
    raise ValueError("Gemini JSON parse failed after retries")
