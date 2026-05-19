"""Entrypoint shim — Dockerfile va deploy `python -u bot.py` chaqiradi.

Asosiy oqim `main.main()` ga forward qilinadi. Hech qanday boshqa kod bu
modul'ni import qilmaydi (FAZA 3 dekompozitsiyasidan keyin).
"""
import asyncio
import logging

from logging_config import setup_logging
from main import main

setup_logging()
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Server stopped by user")
    except Exception:
        logger.exception("❌ Server error")
