"""Paperwork event recording + dashboard endpoints (async aiosqlite)."""
import logging
import time

import aiosqlite
from fastapi import APIRouter

from .db import STATS_DB_PATH, range_seconds

logger = logging.getLogger(__name__)
router = APIRouter()


async def record_paperwork_event(
    group_id, result: str, load_id=None, latency_ms: int = 0, error: str | None = None,
) -> None:
    """Record one paperwork analyze event. Never raises."""
    try:
        async with aiosqlite.connect(STATS_DB_PATH) as db:
            await db.execute(
                "INSERT INTO paperwork_events (ts, group_id, result, load_id, latency_ms, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    str(group_id) if group_id is not None else None,
                    result,
                    str(load_id) if load_id is not None else None,
                    int(latency_ms or 0),
                    error,
                ),
            )
            await db.commit()
    except Exception:
        logger.exception("⚠️ record_paperwork_event failed")


@router.get("/stats/paperwork/summary", include_in_schema=False)
async def paperwork_summary(range: str = "24h"):
    win = range_seconds(range)
    since = int(time.time()) - win if win is not None else None

    async with aiosqlite.connect(STATS_DB_PATH) as db:
        if since is not None:
            async with db.execute(
                "SELECT result, COUNT(*), AVG(latency_ms) FROM paperwork_events WHERE ts >= ? GROUP BY result",
                (since,),
            ) as cur:
                rows = await cur.fetchall()
            async with db.execute(
                "SELECT COUNT(*), AVG(latency_ms), COUNT(DISTINCT group_id) FROM paperwork_events WHERE ts >= ?",
                (since,),
            ) as cur:
                total_row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT result, COUNT(*), AVG(latency_ms) FROM paperwork_events GROUP BY result"
            ) as cur:
                rows = await cur.fetchall()
            async with db.execute(
                "SELECT COUNT(*), AVG(latency_ms), COUNT(DISTINCT group_id) FROM paperwork_events"
            ) as cur:
                total_row = await cur.fetchone()

    by_result = {r[0]: {"count": r[1], "avg_latency_ms": int(r[2] or 0)} for r in rows}
    # Aggregate query har doim 1 ta row qaytaradi (qiymatlari NULL bo'lishi mumkin).
    assert total_row is not None
    total = int(total_row[0] or 0)
    matched = by_result.get("matched", {}).get("count", 0)
    fail_results = ("no_match", "not_bol", "error", "no_loads")
    failed = sum(by_result.get(k, {}).get("count", 0) for k in fail_results)

    return {
        "range": range,
        "total": total,
        "matched": matched,
        "failed": failed,
        "no_match": by_result.get("no_match", {}).get("count", 0),
        "not_bol": by_result.get("not_bol", {}).get("count", 0),
        "no_loads": by_result.get("no_loads", {}).get("count", 0),
        "error": by_result.get("error", {}).get("count", 0),
        "success_rate": round((matched / total) * 100, 1) if total else 0.0,
        "avg_latency_ms": int(total_row[1] or 0),
        "unique_groups": int(total_row[2] or 0),
        "by_result": by_result,
    }


@router.get("/stats/paperwork/recent", include_in_schema=False)
async def paperwork_recent(limit: int = 50):
    limit = max(1, min(int(limit or 50), 500))
    async with aiosqlite.connect(STATS_DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, group_id, result, load_id, latency_ms, error "
            "FROM paperwork_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return {
        "items": [
            {"id": r[0], "ts": r[1], "group_id": r[2], "result": r[3],
             "load_id": r[4], "latency_ms": r[5], "error": r[6]}
            for r in rows
        ]
    }


@router.get("/stats/paperwork/timeseries", include_in_schema=False)
async def paperwork_timeseries(range: str = "24h"):
    win = range_seconds(range) or 2592000
    bucket = 3600 if win <= 86400 else 86400
    since = int(time.time()) - win
    async with aiosqlite.connect(STATS_DB_PATH) as db:
        async with db.execute(
            f"SELECT (ts / {bucket}) * {bucket} AS b, result, COUNT(*) "
            f"FROM paperwork_events WHERE ts >= ? GROUP BY b, result ORDER BY b ASC",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
    buckets: dict[int, dict] = {}
    for b, result, cnt in rows:
        buckets.setdefault(b, {"ts": b, "matched": 0, "no_match": 0, "not_bol": 0, "error": 0, "no_loads": 0})
        if result in buckets[b]:
            buckets[b][result] += cnt
    return {"range": range, "bucket_seconds": bucket, "series": [buckets[k] for k in sorted(buckets)]}
