"""Bot entry point: startup, restoration, worker tasks, uvicorn.

`python -u main.py` (yoki backward-compat'lik uchun `python bot.py`) ishga tushadi.
"""
import asyncio
import logging
from datetime import datetime

import aiosqlite
import uvicorn
from app import app
from config import BOT_PORT, DB_PATH, ERROR_GROUP_ID
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


async def run_bot():
    """Aiogram dispatcher'ni start qiladi."""
    # Late import — telegram/handlers.py state/groups'dan import qiladi (loop bo'lmasin)
    from telegram import telegram_router
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

    worker_task = asyncio.create_task(message_worker())
    logger.info("📨 Message worker started")

    retry_task = asyncio.create_task(retry_failed_messages())
    logger.info("🔁 Failed messages retry worker started")

    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=BOT_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    bot_task = asyncio.create_task(run_bot())

    await asyncio.gather(server.serve(), bot_task, worker_task, retry_task)


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
    async with aiosqlite.connect(DB_PATH) as db:
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
