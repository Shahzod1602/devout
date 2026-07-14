"""In-memory log ring buffer — admin panel "jonli oqim" manbasi.

Root logger'ga ulanadi (setup_logging ichida), oxirgi N ta INFO+ yozuvni
saqlaydi. Hech qachon raise qilmaydi — logging yo'lida exception butun
handlerlar zanjirini buzishi mumkin.
"""
import logging
import threading
import time
from collections import deque

BUFFER_SIZE = 2000

_buffer: deque[dict] = deque(maxlen=BUFFER_SIZE)
_lock = threading.Lock()
_seq = 0  # monotonik id — frontend "shu id'dan keyingilarini ber" uchun


class RingBufferHandler(logging.Handler):
    """Log yozuvlarini dict ko'rinishida ring bufferga qo'shadi."""

    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_text is None:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            entry = {
                "ts": time.time(),
                "level": record.levelname,
                "logger": record.name,
                "msg": msg if len(msg) <= 2000 else msg[:2000] + "…",
            }
            if record.exc_text:
                entry["exc"] = record.exc_text[:3000]
            with _lock:
                _seq += 1
                entry["id"] = _seq
                _buffer.append(entry)
        except Exception:  # noqa: BLE001 — logging yo'lida hech qachon raise qilmaymiz
            pass


def get_logs(after_id: int = 0, level: str | None = None, q: str | None = None, limit: int = 300) -> list[dict]:
    """Buffer'dan filtrlanag'an yozuvlar (eng eskisi birinchi).

    after_id — shu id'dan KEYINGI yozuvlar (inkremental poll uchun).
    level — minimal daraja (INFO/WARNING/ERROR). q — msg/logger substring.
    """
    min_no = logging.getLevelName(level.upper()) if level else 0
    if not isinstance(min_no, int):  # noma'lum daraja nomi
        min_no = 0
    ql = (q or "").lower()
    with _lock:
        snapshot = list(_buffer)
    out = []
    for e in snapshot:
        if e["id"] <= after_id:
            continue
        if min_no and logging.getLevelName(e["level"]) < min_no:
            continue
        if ql and ql not in e["msg"].lower() and ql not in e["logger"].lower():
            continue
        out.append(e)
    return out[-limit:]


def attach_ring_buffer(level: int = logging.INFO) -> RingBufferHandler:
    """Root logger'ga bitta RingBufferHandler ulaydi (ikkinchi marta ulamaydi)."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RingBufferHandler):
            return h
    handler = RingBufferHandler(level=level)
    root.addHandler(handler)
    return handler
