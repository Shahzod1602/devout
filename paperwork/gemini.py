"""Gemini SDK setup + low-level call wrapper for paperwork analysis."""
import asyncio
import json
import logging
import time
from io import BytesIO

from config import GEMINI_BOT_MODEL, VERTEX_LOCATION, VERTEX_PROJECT
from google import genai
from google.genai import types as genai_types
from stats import record_gemini_call

logger = logging.getLogger(__name__)

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


async def gemini_extract_once(pil_images: list, text_prompt: str, attempt_num: int) -> dict:
    """Gemini'dan bir marta ma'lumot olish (cost + latency tracking bilan).

    JSON parse xatosi bo'lsa 3 marta retry qilinadi.
    Tashqi exception holida ham record_gemini_call (success=False) chaqiriladi.
    """
    image_parts = []
    for img in pil_images:
        buf = BytesIO()
        img.save(buf, format="PNG")
        image_parts.append(genai_types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"))

    for retry in range(3):
        loop = asyncio.get_event_loop()
        call_start = time.time()
        response = None
        try:
            response = await loop.run_in_executor(
                None,
                lambda: get_genai_client().models.generate_content(
                    model=GEMINI_BOT_MODEL,
                    # genai-sdk `contents` expects invariant list; mixed str+Part is OK at runtime
                    contents=[text_prompt, *image_parts],  # type: ignore[arg-type]
                    config=genai_types.GenerateContentConfig(max_output_tokens=10000),
                ),
            )
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(GEMINI_BOT_MODEL, response, latency_ms, success=True)
        except Exception:
            latency_ms = int((time.time() - call_start) * 1000)
            await record_gemini_call(GEMINI_BOT_MODEL, response, latency_ms, success=False)
            raise

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
