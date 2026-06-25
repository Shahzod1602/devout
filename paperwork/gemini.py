"""Gemini SDK setup + low-level call wrapper for paperwork analysis."""
import asyncio
import json
import logging
import random
import time
from io import BytesIO

from config import GEMINI_BOT_MODEL, VERTEX_LOCATION, VERTEX_PROJECT
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from stats import record_gemini_call

logger = logging.getLogger(__name__)

# Vertex 429 / RESOURCE_EXHAUSTED (kvota) uchun exponential-backoff retry.
# Google'ning o'z tavsiyasi: 429 da "try again later" — shuning uchun darrov
# yiqitmasdan, jitter bilan bir necha marta qayta urinamiz. Burst paytida
# parallel call'lar jitter tufayli yoyilib, daqiqalik limitga bosimni kamaytiradi.
_QUOTA_MAX_RETRIES = 4          # jami urinishlar (1 asosiy + 3 retry)
_QUOTA_BACKOFF_BASE = 2.0       # 2s, 4s, 8s (+ 0..1s jitter)

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


def _is_quota_error(exc: Exception) -> bool:
    """429 / RESOURCE_EXHAUSTED (Vertex kvota) xatosini aniqlash."""
    if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 429:
        return True
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


async def _generate_with_backoff(text_prompt: str, image_parts: list):
    """generate_content chaqiruvi — 429/RESOURCE_EXHAUSTED bo'lsa backoff bilan retry.

    Har bir urinish (muvaffaqiyatli yoki yo'q) `record_gemini_call` orqali yoziladi.
    Kvota bilan bog'liq bo'lmagan xatolar darrov qayta tashlanadi (retry qilinmaydi).
    """
    loop = asyncio.get_event_loop()
    for quota_retry in range(_QUOTA_MAX_RETRIES):
        call_start = time.time()
        response = None
        try:
            response = await loop.run_in_executor(
                None,
                lambda: get_genai_client().models.generate_content(
                    model=GEMINI_BOT_MODEL,
                    # genai-sdk `contents` expects invariant list; mixed str+Part is OK at runtime
                    contents=[text_prompt, *image_parts],
                    config=genai_types.GenerateContentConfig(max_output_tokens=10000),
                ),
            )
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(GEMINI_BOT_MODEL, response, latency_ms, success=True)
            return response
        except Exception as exc:
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(GEMINI_BOT_MODEL, response, latency_ms, success=False)
            if _is_quota_error(exc) and quota_retry < _QUOTA_MAX_RETRIES - 1:
                delay = _QUOTA_BACKOFF_BASE * (2 ** quota_retry) + random.uniform(0, 1)
                logger.warning(
                    "⏳ Gemini 429 RESOURCE_EXHAUSTED — backoff %.1fs (urinish %d/%d)",
                    delay, quota_retry + 1, _QUOTA_MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue
            raise
    # range tugashi mumkin emas (oxirgi urinishda raise bo'ladi), faqat tip uchun:
    raise RuntimeError("Gemini quota backoff retries exhausted")


async def gemini_extract_once(pil_images: list, text_prompt: str, attempt_num: int) -> dict:
    """Gemini'dan bir marta ma'lumot olish (cost + latency tracking bilan).

    JSON parse xatosi bo'lsa 3 marta retry qilinadi. API chaqiruvi 429/
    RESOURCE_EXHAUSTED (kvota) bilan yiqilsa, `_generate_with_backoff` ichida
    exponential backoff bilan qayta urinadi. Har bir urinish record_gemini_call
    (success=True/False) orqali yoziladi.
    """
    image_parts = []
    for img in pil_images:
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_parts.append(genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

    for retry in range(3):
        response = await _generate_with_backoff(text_prompt, image_parts)
        result = (response.text or "").strip()
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
