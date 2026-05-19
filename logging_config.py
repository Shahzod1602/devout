"""Strukturali logging setup — stdlib `logging.config.dictConfig` orqali.

Environment o'zgaruvchilari:
- LOG_LEVEL: DEBUG/INFO/WARNING/ERROR/CRITICAL (default: INFO)
- LOG_FORMAT: "text" (rangli, dev) yoki "json" (production'ga, bir qatorli) — default: text
- TELEGRAM_LOG_BOT: "1" yoqsa, ERROR+ ni darhol, WARNING ni batched
  (har 10s) ERROR_GROUP_ID'ga yuboradi. Faqat production'da.

main.py'da `setup_logging()` chaqiriladi — barcha modullar `logging.getLogger(__name__)`
orqali olinadi.

Noisy 3rd-party logger'lar (aiohttp, aiogram, uvicorn.access) WARNING ga tushiriladi.

Telegram handler'ni state ready bo'lgach attach qilish (main.py orqali):
    from logging_config import attach_telegram_handler
    handler = attach_telegram_handler(error_bot, ERROR_GROUP_ID)
    await handler.start()
"""
import json
import logging
import logging.config
import os
import sys


class JsonFormatter(logging.Formatter):
    """Bir qatorli JSON — production observability uchun."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Extra context (logger.info(..., extra={"group_id": 123}) ko'rinishida)
        for key, value in record.__dict__.items():
            if key in ("name", "msg", "args", "levelname", "levelno", "pathname",
                       "filename", "module", "exc_info", "exc_text", "stack_info",
                       "lineno", "funcName", "created", "msecs", "relativeCreated",
                       "thread", "threadName", "processName", "process", "message",
                       "taskName"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """Logging'ni boshlash. Ikki marta chaqirilsa, force=True re-config qiladi."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("LOG_FORMAT", "text").lower()

    if fmt == "json":
        formatter = {"()": "logging_config.JsonFormatter"}
    else:
        formatter = {
            "format": "%(asctime)s %(levelname)-7s %(name)-25s | %(message)s",
            "datefmt": "%H:%M:%S",
        }

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": formatter},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "default",
                "level": level,
            },
        },
        "root": {"handlers": ["stdout"], "level": level},
        "loggers": {
            # Noisy 3rd-party'larni jim qilish — bizning kodimizdan
            # signal ko'rinmasligi uchun.
            "aiohttp.access": {"level": "WARNING"},
            "uvicorn.access": {"level": "WARNING"},
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "openai": {"level": "WARNING"},
            "groq": {"level": "WARNING"},
        },
    })


def attach_telegram_handler(bot, chat_id: int | str, **kwargs):
    """Attach TelegramLogHandler to root logger. Returns handler (caller awaits start()).

    Env vars (debug/test rejimi uchun):
    - TELEGRAM_LOG_BOT=0 → handler ulanmaydi (opt-out)
    - TELEGRAM_LOG_LEVEL=INFO/WARNING/ERROR (default WARNING) — handler qabul qiladigan minimum
    - TELEGRAM_LOG_INSTANT_LEVEL=ERROR/CRITICAL (default ERROR) — darhol yuboriladigan minimum
    - TELEGRAM_LOG_INCLUDE_FRAMEWORK=1 → aiogram/uvicorn/httpx ham yuboriladi (default 0 = filter)
    - TELEGRAM_LOG_BATCH_INTERVAL=10 (sekund) — batched yuborishlar oraliq
    - TELEGRAM_LOG_MAX_BATCH=50 — bitta xabarda max log soni

    ⚠️ TELEGRAM_LOG_LEVEL=INFO + TELEGRAM_LOG_INCLUDE_FRAMEWORK=1 = 20 msg/daqiqa
       limit'idan oshib ketishi mumkin. Faqat qisqa test uchun.
    """
    if os.environ.get("TELEGRAM_LOG_BOT", "1").lower() in ("0", "false", "no"):
        return None

    # Late import — module-level import qilsak, aiogram/state'gacha cycle bo'lishi mumkin.
    from telegram_log_handler import TelegramLogHandler  # noqa: PLC0415

    level_name = os.environ.get("TELEGRAM_LOG_LEVEL", "WARNING").upper()
    instant_level_name = os.environ.get("TELEGRAM_LOG_INSTANT_LEVEL", "ERROR").upper()
    include_framework = os.environ.get("TELEGRAM_LOG_INCLUDE_FRAMEWORK", "0").lower() in ("1", "true", "yes")
    batch_interval = float(os.environ.get("TELEGRAM_LOG_BATCH_INTERVAL", "10"))
    max_batch_size = int(os.environ.get("TELEGRAM_LOG_MAX_BATCH", "50"))

    handler_kwargs: dict = {
        "level": getattr(logging, level_name, logging.WARNING),
        "instant_level": getattr(logging, instant_level_name, logging.ERROR),
        "batch_interval": batch_interval,
        "max_batch_size": max_batch_size,
    }
    if include_framework:
        # Bo'sh tuple → hech qanday logger filter qilinmaydi
        handler_kwargs["framework_prefixes"] = ()

    handler_kwargs.update(kwargs)
    handler = TelegramLogHandler(bot=bot, chat_id=chat_id, **handler_kwargs)
    logging.getLogger().addHandler(handler)
    return handler
