"""Bot entry point: startup, restoration, worker tasks, uvicorn.

`python -u main.py` (yoki backward-compat'lik uchun `python bot.py`) ishga tushadi.
"""
import asyncio
import logging
import signal
from datetime import datetime

import uvicorn
from app import app
from config import BOT_PORT, DB_PATH, ERROR_GROUP_ID
from db.connect import db_connect
from external import get_api_token
from groups import load_all_group_tokens, load_started_groups
from logging_config import attach_telegram_handler, setup_logging
from messaging import message_worker
from state import (
    GROUP_DRIVER_IDS,
    GROUP_TEAM_DRIVERS,
    GROUP_TICKET_POLLING_TASKS,
    GROUP_TICKET_STATUS,
    STARTED_GROUPS,
    bot,
    dp,
    error_bot,
)
from stats import init_stats_db
from tickets import poll_backend_ticket_status, retry_failed_messages

logger = logging.getLogger(__name__)

# Fire-and-forget fon task'lariga ref (GC'lanmasin) — #19 blob-prune shu yerda.
_BG_TASKS: set = set()


async def run_bot():
    """Aiogram dispatcher'ni start qiladi."""
    # Late import — telegram/handlers.py state/groups'dan import qiladi (loop bo'lmasin)
    from telegram import telegram_router
    from telegram.chat_logger import ChatLogMiddleware
    dp.message.outer_middleware(ChatLogMiddleware())
    dp.include_router(telegram_router)
    logger.info("🤖 Telegram bot is starting...")
    await get_api_token()
    await dp.start_polling(bot)


async def main():
    """Restart state'ni tiklash, worker'larni start qilish, FastAPI + bot'ni gather qilish."""
    logger.info("🚀 Starting combined Telegram bot and FastAPI server...")
    logger.info("📊 Available endpoints:")
    logger.info("   - Telegram bot: Listening for messages")
    logger.info("   - BOT API: http://localhost:%d/docs", BOT_PORT)
    logger.info("   - BOL Check: POST http://localhost:%d/check-bol", BOT_PORT)

    await _initialize_state()

    # Telegram log handler — ERROR+ darhol, WARNING batched (har 10s).
    # state.error_bot allaqachon mavjud (import time'da yaratilgan).
    tg_log_handler = attach_telegram_handler(error_bot, ERROR_GROUP_ID)
    if tg_log_handler is not None:
        await tg_log_handler.start()
        logger.info("📡 Telegram log handler started (ERROR instant, WARNING batched 10s)")

    # #17 (audit v3): bir nechta message worker — burst'da (masalan bitta company'ning
    # ko'p guruhiga paperwork notify) xabarlar parallel yuboriladi; bittalik worker sekin edi.
    NUM_MESSAGE_WORKERS = 3
    worker_tasks = [asyncio.create_task(message_worker()) for _ in range(NUM_MESSAGE_WORKERS)]
    logger.info("📨 %d message worker started", NUM_MESSAGE_WORKERS)

    retry_task = asyncio.create_task(retry_failed_messages())
    logger.info("🔁 Failed messages retry worker started")

    # #19: eskirgan BOL/POD blob'larni FON'da tozalaymiz — katta DB'da (prod 2.7GB)
    # bloklovchi prune startup/health-gate'ni ushlab qolmasin. Bir martalik, idempotent.
    # Fire-and-forget naqsh: set'da ref saqlaymiz (GC'lanmasin), tugagach o'zini olib tashlaydi.
    from db.operations import prune_expired_blobs
    _bg = asyncio.create_task(prune_expired_blobs())
    _BG_TASKS.add(_bg)
    _bg.add_done_callback(_BG_TASKS.discard)

    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=BOT_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    bot_task = asyncio.create_task(run_bot())

    # #18 (audit v3) GRACEFUL SHUTDOWN: SIGTERM (docker stop) / SIGINT darhol o'ldirmasin —
    # message_queue'ni drain, Telegram log buffer'ini flush, bot sessiyalarini yopamiz.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(_sig, stop_event.set)
        except NotImplementedError:
            # Signalsiz platform (masalan Windows) — __main__ dagi KeyboardInterrupt fallback.
            pass

    background = [serve_task, bot_task, retry_task, *worker_tasks]
    # Shutdown signali kelguncha YOKI biror background task tugab qolguncha ishlaymiz.
    await asyncio.wait(
        [*background, asyncio.ensure_future(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )
    await _graceful_shutdown(server, serve_task, bot_task, retry_task, worker_tasks, tg_log_handler)


async def _graceful_shutdown(server, serve_task, bot_task, retry_task, worker_tasks, tg_log_handler):
    """SIGTERM/SIGINT yoki task tugashida yumshoq to'xtash (audit v3 #18).

    Tartib: uvicorn'ni to'xtatish → navbatdagi xabarlarni yuborishga ulgurish (bounded)
    → Telegram log buffer flush → task'larni bekor qilish → bot sessiyalarini yopish.
    """
    from state import message_queue

    logger.info("🛑 Shutdown boshlandi — message_queue drain qilinmoqda...")
    # 1) Uvicorn'ni yumshoq to'xtatamiz (in-flight so'rovlar tugasin, yangisi qabul qilinmasin).
    server.should_exit = True
    # 2) Navbatdagi xabarlarni yuborishga imkon beramiz (worker'lar hali tirik) — 10s cheklov.
    try:
        await asyncio.wait_for(message_queue.join(), timeout=10)
        logger.info("✅ Message queue drained")
    except TimeoutError:
        logger.warning("⚠️ Queue drain timeout (10s) — %d xabar yuborilmay qoldi", message_queue.qsize())
    # 3) Telegram log buffer'ini flush + worker'ni to'xtatamiz (stop() ichida final flush bor).
    if tg_log_handler is not None:
        try:
            await tg_log_handler.stop()
        except Exception:
            logger.debug("tg_log_handler.stop() failed", exc_info=True)
    # 4) Qolgan barcha task'larni bekor qilamiz.
    for _t in (bot_task, retry_task, serve_task, *worker_tasks):
        _t.cancel()
    # 5) Bot HTTP sessiyalarini yopamiz (aiohttp connector leak bo'lmasin).
    for _b in (bot, error_bot):
        try:
            await _b.session.close()
        except Exception:
            logger.debug("bot.session.close() failed", exc_info=True)
    logger.info("👋 Graceful shutdown tugadi")


async def _initialize_state():
    """DB + JSON cache'lardan restart state'ni RAM ga tiklash."""
    from db import init_db
    await init_db()
    await init_stats_db()

    # /start bosilgan guruhlar
    STARTED_GROUPS.update(load_started_groups())
    if STARTED_GROUPS:
        logger.info("♻️ Restored %d started groups", len(STARTED_GROUPS))

    # DB dan driver'lar
    async with db_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT group_id, driver_id, driver_name, team_driver_id, team_driver_name "
            "FROM groups WHERE driver_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            for group_id, driver_id, driver_name, team_driver_id, team_driver_name in rows:
                GROUP_DRIVER_IDS[group_id] = driver_id
                logger.info("♻️ Restored driver %s (ID: %s) for group %s", driver_name, driver_id, group_id)
                if team_driver_id:
                    GROUP_TEAM_DRIVERS[group_id] = team_driver_id
                    logger.info("♻️ Restored team driver %s (ID: %s) for group %s",
                                team_driver_name, team_driver_id, group_id)

    # JSON cache'dan "todo" ticket status'lari + polling task'lari
    all_data = load_all_group_tokens()
    for group_id, group_data in all_data.items():
        saved_status = group_data.get("ticket_status", "")
        if saved_status == "todo":
            updated_at_str = group_data.get("ticket_status_updated_at")
            GROUP_TICKET_STATUS[group_id] = {
                "status": "todo",
                "created_at": updated_at_str or datetime.now().isoformat(),
                "done_at": None,
            }
            polling_task = asyncio.create_task(poll_backend_ticket_status(group_id))
            GROUP_TICKET_POLLING_TASKS[group_id] = polling_task
            logger.info("♻️ Restored ticket status 'todo' for group %s", group_id)


if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Server stopped by user")
    except Exception:
        logger.exception("❌ Server error")
