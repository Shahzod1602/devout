"""FastAPI ilovasi: lifespan, static UI mount, router'larni include qilish.

`main.py` shu app'ni uvicorn orqali ishga tushiradi.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from api import api_router
from api.admin import router as admin_router
from api.ai_chat import router as ai_chat_router
from db import init_db
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from log_buffer import attach_ring_buffer
from paperwork import paperwork_api_router
from stats import gemini_router, init_stats_db, paperwork_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 FastAPI starting up...")
    # Idempotent init'lar — main.py ham chaqiradi, lekin `uvicorn app:app` to'g'ridan
    # ishga tushirilsa ham admin panel (checkin_events jadvali, log ring buffer) ishlasin.
    attach_ring_buffer()
    await init_db()
    await init_stats_db()
    from main import run_bot  # late import — main.py o'z navbatida app'ni import qiladi
    bot_task = asyncio.create_task(run_bot())
    logger.info("🤖 Telegram bot started in background")
    yield
    logger.info("🛑 FastAPI shutting down...")
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        logger.info("🤖 Telegram bot stopped")


app = FastAPI(
    title="Telegram Group Message API",
    description="Bu API orqali **groupId** yuborib Telegram guruhiga xabar jo'natish mumkin.",
    version="1.0.0",
    lifespan=lifespan,
)

# Static UI mount (paperwork stats dashboard)
_static_candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
    os.path.join(os.getcwd(), "static"),
]
for _sd in _static_candidates:
    if os.path.isdir(_sd):
        app.mount("/ui", StaticFiles(directory=_sd, html=True), name="ui")
        break

# Router'larni include qilish
app.include_router(paperwork_router)         # /stats/paperwork/*
app.include_router(gemini_router)            # /stats/gemini/*
app.include_router(paperwork_api_router)     # /check-bol
app.include_router(api_router)               # /send-message, /permissions/*, /accepted, ...
app.include_router(admin_router)             # /admin — token-auth kuzatuv paneli
app.include_router(ai_chat_router)           # /api/ai/chat — chat-log + guruh tahlili (AI_API_TOKEN)


@app.get("/health", include_in_schema=False)
async def health():
    """Liveness probe — deploy healthcheck shu endpoint'ni uradi (audit v3 #5)."""
    return {"status": "ok"}
