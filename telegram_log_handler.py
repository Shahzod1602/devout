"""Telegram log handler — ERROR+ ni darhol, WARNING ni batched yuboradi.

Architecture:
- `emit()` sync chaqiriladi (logger qaerdan kelishi bilan), record'ni
  thread-safe buffer'larga qo'shadi.
- Background asyncio worker har 10s'da WARNING buffer'ni flush qiladi,
  ERROR/CRITICAL'larni darhol (har 1s polling).
- Telegram xato bersa silently drop (recursion'ni oldini olish).
- Framework noise (aiogram.*, uvicorn.*, httpx, ...) filterga uchraydi.

Foydalanish:
    from telegram_log_handler import TelegramLogHandler
    handler = TelegramLogHandler(bot=error_bot, chat_id=ERROR_GROUP_ID)
    logging.getLogger().addHandler(handler)
    # main()'da:
    await handler.start()
    # shutdown:
    await handler.stop()
"""
from __future__ import annotations

import asyncio
import html
import logging
import threading
from datetime import datetime

# Bu logger'lar shovqinli — qabul qilinmaydi (rate limit'ni urmasligi uchun).
_FRAMEWORK_LOGGER_PREFIXES = (
    "aiogram",
    "aiohttp",
    "uvicorn",
    "httpx",
    "httpcore",
    "openai",
    "groq",
    "asyncio",
)

# Telegram message hard limit: 4096 chars. Batched message uchun zaxira qoldiramiz.
_MAX_MESSAGE_LEN = 3800

_LEVEL_EMOJI = {
    logging.DEBUG: "🐛",
    logging.INFO: "ℹ️",
    logging.WARNING: "⚠️",
    logging.ERROR: "❌",
    logging.CRITICAL: "🔥",
}


class TelegramLogHandler(logging.Handler):
    """Logger handler that ships ERROR+ instantly and WARNING+ batched to Telegram."""

    def __init__(
        self,
        bot,                     # aiogram.Bot
        chat_id: int | str,
        *,
        level: int = logging.WARNING,
        instant_level: int = logging.ERROR,
        batch_interval: float = 10.0,
        max_batch_size: int = 50,
        framework_prefixes: tuple[str, ...] = _FRAMEWORK_LOGGER_PREFIXES,
    ) -> None:
        super().__init__(level=level)  # filter at handler level
        self.bot = bot
        self.chat_id = chat_id
        self.instant_level = instant_level
        self.batch_interval = batch_interval
        self.max_batch_size = max_batch_size
        self.framework_prefixes = framework_prefixes

        # Thread-safe buffers — emit() sync chaqiriladi.
        self._lock = threading.Lock()
        self._instant_buffer: list[logging.LogRecord] = []
        self._batch_buffer: list[logging.LogRecord] = []

        self._worker_task: asyncio.Task | None = None
        self._stopping = False
        # Internal logger — bu handler'dan log yozsa, infinite loop bo'lmasin.
        self._internal_logger = logging.getLogger("telegram_log_handler")
        self._internal_logger.addFilter(_SkipSelfFilter())

    # ===== Sync API (logger calls this) =====

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # 1) Framework noise — skip
            if any(record.name.startswith(p) for p in self.framework_prefixes):
                return
            # 2) Recursion guard — never ship our own logger
            if record.name.startswith("telegram_log_handler"):
                return
            # 3) Severity sort
            with self._lock:
                if record.levelno >= self.instant_level:
                    self._instant_buffer.append(record)
                else:
                    self._batch_buffer.append(record)
        except Exception:
            self.handleError(record)

    # ===== Async lifecycle =====

    async def start(self) -> None:
        """Background worker'ni ishga tushirish. Idempotent."""
        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._worker_task = asyncio.create_task(self._worker(), name="telegram-log-worker")

    async def stop(self) -> None:
        """Workerni to'xtatish, qolgan log'larni final flush qilish."""
        self._stopping = True
        if self._worker_task is not None:
            await self._flush()  # final flush before cancel
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

    # ===== Internal =====

    async def _worker(self) -> None:
        """Forever loop: instant'larni 1s'da, batch'ni `batch_interval`'da yuboradi."""
        elapsed = 0.0
        poll_interval = 1.0
        try:
            while not self._stopping:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                # Instant: ERROR+
                await self._send_instant()

                # Batched: WARNING — har `batch_interval`'da
                if elapsed >= self.batch_interval:
                    await self._send_batch()
                    elapsed = 0.0
        except asyncio.CancelledError:
            raise
        except Exception:
            # Worker hech qachon o'lib qolmasin — log + davom etish.
            try:
                self._internal_logger.exception("TelegramLogHandler worker error")
            except Exception:
                pass

    async def _send_instant(self) -> None:
        with self._lock:
            # EXT-5: error storm'da yuzlab xabar Telegram'ga toshib ketmasin — sikl uchun cheklaymiz.
            records = self._instant_buffer[: self.max_batch_size]
            dropped = len(self._instant_buffer) - len(records)
            self._instant_buffer.clear()
        for record in records:
            await self._send(self._format_record(record))
        if dropped > 0:
            await self._send(f"… +{dropped} more ERROR log(s) suppressed (flood protection)")

    async def _send_batch(self) -> None:
        with self._lock:
            records = self._batch_buffer[: self.max_batch_size]
            del self._batch_buffer[: self.max_batch_size]
        if not records:
            return
        msg = self._format_batch(records)
        await self._send(msg)

    async def _flush(self) -> None:
        """Buffer'lardagi hammasini darhol jo'natish (cancel oldidan)."""
        await self._send_instant()
        await self._send_batch()

    async def _send(self, text: str) -> None:
        """Telegram'ga jo'natish — xato bo'lsa silently drop."""
        if not text:
            return
        if len(text) > _MAX_MESSAGE_LEN:
            text = text[:_MAX_MESSAGE_LEN] + "\n... (truncated)"
        try:
            await self.bot.send_message(self.chat_id, text, parse_mode="HTML")
        except Exception:
            # Hech qanday loglash yo'q — recursion va shovqin oldini olish uchun.
            pass

    # ===== Formatting =====

    def _format_record(self, record: logging.LogRecord) -> str:
        emoji = _LEVEL_EMOJI.get(record.levelno, "•")
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        msg = html.escape(record.getMessage())
        parts = [f"{emoji} <b>{record.levelname}</b> <code>{html.escape(record.name)}</code> {ts}", msg]
        if record.exc_info:
            tb = logging.Formatter().formatException(record.exc_info)
            tb_short = "\n".join(tb.splitlines()[-8:])  # oxirgi 8 qator
            parts.append(f"<pre>{html.escape(tb_short)}</pre>")
        return "\n".join(parts)

    def _format_batch(self, records: list[logging.LogRecord]) -> str:
        ts = datetime.now().strftime("%H:%M:%S")
        header = f"⚠️ <b>WARNINGS</b> (last {self.batch_interval:.0f}s) — {len(records)} events @ {ts}"
        lines = [header]
        for r in records:
            t = datetime.fromtimestamp(r.created).strftime("%H:%M:%S")
            msg = html.escape(r.getMessage())
            lines.append(f"• [{t}] <code>{html.escape(r.name)}</code> {msg}")
        return "\n".join(lines)


class _SkipSelfFilter(logging.Filter):
    """Filter out logs from ourselves to prevent recursion."""
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("telegram_log_handler")
