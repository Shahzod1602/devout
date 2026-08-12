"""LLM matn-yo'lagi (OpenAI-mos `chat/completions`) — guruh chat tahlili uchun.

Nega Gemini emas: guruh tahlili uzun transkript yeydi va **burst** bo'lishi mumkin.
Gemini yo'lagi (`paperwork/gemini.py`) paperwork bilan BIR XIL Vertex kvotasini
bo'lishadi — 2026-07-10 darsi (askai ratecon burst'i paperwork'ni 429 bilan
yiqitgan) shuni ko'rsatdi. Bu yo'lak boshqa provayderda: tahlil qancha
ishlatilmasin paperwork'ning retry-oynasiga tegmaydi.

DEFAULT provayder — **opencode.ai/zen** gateway (OpenAI-mos), model
`deepseek-v4-flash-free` (bepul tier; 2026-08-11 o'lchov: 60k belgi transkript
~11s, dispatch-muammolarini sana+muallif bilan to'g'ri ajratdi).

Boshqa provayderga o'tish faqat env bilan (kod-deploy'siz), masalan Z.ai:
  LLM_ENDPOINT=https://api.z.ai/api/paas/v4/chat/completions
  LLM_MODEL=glm-5.2   LLM_THINKING=disabled   LLM_API_KEY=<z.ai kalit>

Statistika: har chaqiruv `record_gemini_call` orqali o'sha `gemini_calls`
jadvaliga yoziladi — admin paneldagi xarajat ko'rinishi bitta joyda qoladi
(endpoint yorlig'i `group-analyze`). Narxlar `stats/gemini.py` da; `*-free`
modellar $0.
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from config import (
    LLM_API_KEY,
    LLM_ENDPOINT,
    LLM_MAX_CONCURRENCY,
    LLM_MODEL,
    LLM_THINKING,
    LLM_TIMEOUT_S,
    ssl_context,
)
from stats.gemini import record_gemini_call

logger = logging.getLogger(__name__)

# Bir vaqtda ochiq chaqiruvlar — bot'ni ham, provayder rate-limitini ham himoya qiladi.
_SEM = asyncio.Semaphore(max(1, LLM_MAX_CONCURRENCY))

_MAX_RETRIES = 3           # 1 asosiy + 2 qayta urinish
_BACKOFF_S = (2.0, 5.0)    # faqat 429/5xx uchun


class LlmError(RuntimeError):
    """LLM chaqiruvi muvaffaqiyatsiz (kalit yo'q, timeout, retry'lar tugadi)."""


class _UsageShim:
    """`record_gemini_call` Gemini SDK javobini kutadi (usage_metadata.*_token_count).
    OpenAI-uslub `usage` bloki shu nomlarga o'giriladi."""

    def __init__(self, usage: dict):
        self.prompt_token_count = int(usage.get("prompt_tokens") or 0)
        self.candidates_token_count = int(usage.get("completion_tokens") or 0)
        self.total_token_count = int(
            usage.get("total_tokens") or (self.prompt_token_count + self.candidates_token_count)
        )


class _ResponseShim:
    def __init__(self, usage: dict):
        self.usage_metadata = _UsageShim(usage)


# Z.ai "Insufficient balance or no resource package" ni HTTP **429** bilan qaytaradi —
# throttle emas, doimiy holat. Qayta urinish faqat vaqt yo'qotadi (3 × 90s), shuning
# uchun tanani ham tekshiramiz.
_PERMANENT_CODES = ("1113",)


def _is_retryable(status: int, body: str = "") -> bool:
    if status == 429:
        return not any(f'"{c}"' in body or f"'{c}'" in body for c in _PERMANENT_CODES)
    return status >= 500


async def llm_chat(
    system: str,
    prompt: str,
    *,
    model: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    endpoint_label: str = "llm",
) -> tuple[str, dict]:
    """Bitta chat chaqiruvi. `(javob_matni, usage)` qaytaradi, xatoda `LlmError`.

    `usage` — provayderning xom bloki (`prompt_tokens`/`completion_tokens`/`total_tokens`).
    """
    if not LLM_API_KEY:
        raise LlmError("LLM_API_KEY o'rnatilmagan")

    mdl = (model or LLM_MODEL).strip()
    payload: dict = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if LLM_THINKING in ("disabled", "enabled"):
        # Z.ai-ga xos maydon (thinking o'chirilsa output token/narx kamayadi).
        # Default BO'SH — opencode.ai/zen bu maydonni kutmaydi, shuning uchun
        # faqat aniq so'ralganda yuboriladi.
        payload["thinking"] = {"type": LLM_THINKING}

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
        # Aniq UA — opencode.ai oldidagi Cloudflare ba'zi kutubxona-UA'larini
        # bloklaydi (urllib 1010 bilan rad etilgan; aiohttp o'tgan, lekin
        # bunga tayanmaymiz).
        "User-Agent": "updater-bot/1.0",
    }
    timeout = aiohttp.ClientTimeout(total=LLM_TIMEOUT_S)
    last_err = "noma'lum xato"

    async with _SEM:
        for attempt in range(_MAX_RETRIES):
            started = time.monotonic()
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=ssl_context)
                ) as session:
                    async with session.post(
                        LLM_ENDPOINT, json=payload, headers=headers, timeout=timeout
                    ) as resp:
                        body = await resp.text()
                        latency_ms = int((time.monotonic() - started) * 1000)
                        if resp.status != 200:
                            last_err = f"HTTP {resp.status}: {body[:300]}"
                            await record_gemini_call(
                                mdl, None, latency_ms, success=False, endpoint=endpoint_label
                            )
                            if _is_retryable(resp.status, body) and attempt < _MAX_RETRIES - 1:
                                delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
                                logger.warning(
                                    "llm_chat: %s — %.0fs dan keyin qayta urinish (%d/%d)",
                                    last_err, delay, attempt + 1, _MAX_RETRIES,
                                )
                                await asyncio.sleep(delay)
                                continue
                            raise LlmError(last_err)

                        data = await resp.json(content_type=None)

                choices = data.get("choices") or []
                text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
                usage = data.get("usage") or {}
                await record_gemini_call(
                    mdl, _ResponseShim(usage), latency_ms, success=True, endpoint=endpoint_label
                )
                if not text.strip():
                    # Ba'zi reasoning modellar butun javobni `reasoning_content`ga
                    # yozib, `content`ni bo'sh qoldiradi — bu yaroqsiz natija.
                    raise LlmError("model bo'sh javob qaytardi")
                return text.strip(), usage

            except (TimeoutError, aiohttp.ClientError) as e:
                latency_ms = int((time.monotonic() - started) * 1000)
                last_err = f"{type(e).__name__}: {e}"
                await record_gemini_call(
                    mdl, None, latency_ms, success=False, endpoint=endpoint_label
                )
                if attempt < _MAX_RETRIES - 1:
                    delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
                    logger.warning(
                        "llm_chat: %s — %.0fs dan keyin qayta urinish (%d/%d)",
                        last_err, delay, attempt + 1, _MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise LlmError(last_err) from e

    raise LlmError(last_err)
