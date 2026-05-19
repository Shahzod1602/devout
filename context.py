"""AppContext — singleton wrapper for runtime clients and configuration.

Bu modul bot'ning runtime resurslari (aiogram bot/dp, OpenAI client'lar, DB path,
http session pool va h.k.) bir joyga jamlaydi. Yangi kod uchun (testlar,
FastAPI handler'lar) dependency injection sifatida ishlatish mumkin:

    from context import get_context
    ctx = get_context()
    await ctx.openai.chat.completions.create(...)

Mavjud `state.py` (mutable global dict'lar) o'rnini bosmaydi — u runtime
state uchun, AppContext esa **immutable clients + config snapshot**.

FastAPI'da Depends() pattern:

    from fastapi import Depends
    from context import get_context, AppContext

    @router.get(...)
    async def handler(ctx: AppContext = Depends(get_context)):
        ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from aiogram import Bot, Dispatcher
from config import (
    BOT_TOKEN,
    DB_PATH,
    ERROR_BOT_TOKEN,
    GROQ_API_KEY,
    OPENAI_API_KEY,
)
from openai import OpenAI


@dataclass(frozen=True)
class AppContext:
    """Immutable snapshot of runtime clients and configuration.

    Faqat **singleton resurslar** — mutable state (REGISTERED_GROUPS, ...) shu
    yerga emas, balki `state.py`'da.
    """
    # Telegram
    bot: Bot
    dp: Dispatcher
    error_bot: Bot
    # LLM clients
    openai: OpenAI
    groq: OpenAI
    # DB
    db_path: Path
    # Pricing / config snapshot (kelajakda — gemini_pricing, model names, va h.k.)
    extra: dict = field(default_factory=dict)


@lru_cache(maxsize=1)
def get_context() -> AppContext:
    """Singleton accessor. lru_cache idempotency'ni ta'minlaydi.

    `state.py`'dagi clients bilan bir xil obyektlarni qaytaradi (legacy callers
    `state.bot` chaqirsa, `ctx.bot is state.bot` — True).
    """
    import state  # late import — state.py o'z navbatida config'dan import qiladi

    return AppContext(
        bot=state.bot,
        dp=state.dp,
        error_bot=state.error_bot,
        openai=state.openai_client,
        groq=state.groq_client,
        db_path=DB_PATH,
    )


def build_context_for_test(
    *,
    bot: Bot | None = None,
    dp: Dispatcher | None = None,
    error_bot: Bot | None = None,
    openai: OpenAI | None = None,
    groq: OpenAI | None = None,
    db_path: Path | None = None,
) -> AppContext:
    """Test helper — har bir argumentni override qilish (default'lar: stub'lar).

    Production'da `get_context()` ishlatish kerak; bu funksiya faqat
    unit testlar uchun.
    """
    return AppContext(
        bot=bot or Bot(token=BOT_TOKEN),
        dp=dp or Dispatcher(),
        error_bot=error_bot or Bot(token=ERROR_BOT_TOKEN),
        openai=openai or OpenAI(api_key=OPENAI_API_KEY),
        groq=groq or OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1"),
        db_path=db_path or DB_PATH,
    )
