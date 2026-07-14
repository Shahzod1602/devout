"""Checkin/checkout event recording + so'rovlar (admin panel uchun).

Bu modulda FastAPI router YO'Q — o'qish funksiyalari api/admin.py'dagi
auth-himoyalangan endpointlar orqali chaqiriladi (paperwork/gemini stats'dan
farqli: yangi ma'lumotni authsiz ochmaymiz, audit H1 konteksti).

result qiymatlari:
- "sent"          — backend qabul qildi
- "invalid_time"  — vaqt parse bo'lmadi, driver'dan qayta so'raldi
- "missing_time"  — checkin yoki checkout umuman yo'q, driver'dan so'raldi
- "backend_error" — backend 4xx/5xx (status error ustunida)
- "server_down"   — transport xato / 502-504 (jim o'tkazildi)
- "no_permission" — kompaniyada checkInCheckOut o'chiq

checkin_raw/checkout_raw — driver yozgan XOM vaqt stringlari (TZ fix monitoringi:
regex TZ ni saqlayaptimi, qaysi formatlar kelayapti — shu yerdan ko'rinadi).
"""
import logging
import time

from db.connect import db_connect

from .db import STATS_DB_PATH, range_seconds

logger = logging.getLogger(__name__)


async def record_checkin_event(
    group_id, result: str, load_id=None, doc_type=None,
    checkin_raw=None, checkout_raw=None, tz_enum=None, error: str | None = None,
) -> None:
    """Record one checkin/checkout attempt. Never raises."""
    try:
        async with db_connect(STATS_DB_PATH) as db:
            await db.execute(
                "INSERT INTO checkin_events "
                "(ts, group_id, load_id, doc_type, result, checkin_raw, checkout_raw, tz_enum, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    str(group_id) if group_id is not None else None,
                    str(load_id) if load_id is not None else None,
                    doc_type,
                    result,
                    str(checkin_raw)[:100] if checkin_raw is not None else None,
                    str(checkout_raw)[:100] if checkout_raw is not None else None,
                    tz_enum,
                    error[:300] if error else None,
                ),
            )
            await db.commit()
    except Exception:
        logger.exception("⚠️ record_checkin_event failed")


async def checkin_summary(range: str = "24h"):
    """24h/7d checkin natijalar kesimi. Admin router chaqiradi (auth o'sha yerda)."""
    win = range_seconds(range)
    since = int(time.time()) - win if win is not None else 0
    async with db_connect(STATS_DB_PATH) as db:
        async with db.execute(
            "SELECT result, COUNT(*) FROM checkin_events WHERE ts >= ? GROUP BY result",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
    by_result = {r[0]: r[1] for r in rows}
    total = sum(by_result.values())
    return {
        "range": range,
        "total": total,
        "sent": by_result.get("sent", 0),
        "by_result": by_result,
    }


async def checkin_timeseries(range: str = "7d"):
    """Kunlik/soatlik checkin bucket'lari. Admin router chaqiradi."""
    win = range_seconds(range) or 2592000
    bucket = 3600 if win <= 86400 else 86400
    since = int(time.time()) - win
    async with db_connect(STATS_DB_PATH) as db:
        async with db.execute(
            f"SELECT (ts / {bucket}) * {bucket} AS b, result, COUNT(*) "
            f"FROM checkin_events WHERE ts >= ? GROUP BY b, result ORDER BY b ASC",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
    buckets: dict[int, dict] = {}
    for b, result, cnt in rows:
        buckets.setdefault(b, {"ts": b, "sent": 0, "rejected": 0, "failed": 0})
        if result == "sent":
            buckets[b]["sent"] += cnt
        elif result in ("invalid_time", "missing_time"):
            buckets[b]["rejected"] += cnt
        else:
            buckets[b]["failed"] += cnt
    return {"range": range, "bucket_seconds": bucket, "series": [buckets[k] for k in sorted(buckets)]}


async def checkin_recent(limit: int = 50):
    """Oxirgi N checkin urinishi (xom vaqt stringlari bilan). Admin router chaqiradi."""
    limit = max(1, min(int(limit or 50), 500))
    async with db_connect(STATS_DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, group_id, load_id, doc_type, result, checkin_raw, checkout_raw, tz_enum, error "
            "FROM checkin_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return {
        "items": [
            {"id": r[0], "ts": r[1], "group_id": r[2], "load_id": r[3], "doc_type": r[4],
             "result": r[5], "checkin_raw": r[6], "checkout_raw": r[7], "tz_enum": r[8], "error": r[9]}
            for r in rows
        ]
    }
