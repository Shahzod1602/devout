"""Gemini API cost / latency tracking + dashboard endpoints (async aiosqlite)."""
import logging
import time
from contextvars import ContextVar

from db.connect import db_connect
from fastapi import APIRouter

from .db import STATS_DB_PATH, range_seconds

logger = logging.getLogger(__name__)
router = APIRouter()

# Pricing per 1M tokens (input, output) — Google narxlarini yangilab turing.
GEMINI_PRICING_PER_1M = [
    ("gemini-3.5-flash", 1.50, 9.00),
    ("gemini-3-flash",   0.50, 3.00),
    ("gemini-3-pro",     2.00, 12.00),
    ("gemini-2.5-pro",   1.25, 10.00),
    ("gemini-2.5-flash", 0.30, 2.50),
    ("gemini-2.0-flash", 0.10, 0.40),
]

# Joriy chaqiruv kontekstidagi endpoint nomini ushlash uchun (validate-bol, us-mail-analyze, ...).
current_endpoint: ContextVar = ContextVar("current_gemini_endpoint", default="-")

# DBS-4: narx jadvalida yo'q model uchun ogohlantirish (har model uchun bir marta).
_WARNED_MODELS: set = set()


def calc_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    m = (model or "").lower()
    for key, in_price, out_price in GEMINI_PRICING_PER_1M:
        if key in m:
            return (in_tokens * in_price + out_tokens * out_price) / 1_000_000
    # Noma'lum model → $0 (jimgina emas): jadvalga qo'shish kerakligini bildiramiz.
    if model and model not in _WARNED_MODELS:
        _WARNED_MODELS.add(model)
        logger.warning("calc_cost: noma'lum model '%s' — narx $0 hisoblandi (GEMINI_PRICING_PER_1M ga qo'shing)", model)
    return 0.0


async def record_gemini_call(
    model: str, response, latency_ms: int, success: bool = True, endpoint: str | None = None,
) -> None:
    """Record one Gemini API call to the stats DB. Never raises."""
    try:
        in_tok = out_tok = total_tok = 0
        usage = getattr(response, "usage_metadata", None) if response is not None else None
        if usage is not None:
            in_tok = int(getattr(usage, "prompt_token_count", 0) or 0)
            out_tok = int(getattr(usage, "candidates_token_count", 0) or 0)
            total_tok = int(getattr(usage, "total_token_count", 0) or (in_tok + out_tok))
        cost = calc_cost(model, in_tok, out_tok)
        ep = endpoint or current_endpoint.get() or "-"
        async with db_connect(STATS_DB_PATH) as db:
            await db.execute(
                "INSERT INTO gemini_calls (ts, endpoint, model, input_tokens, output_tokens, total_tokens, cost_usd, latency_ms, success) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), ep, model, in_tok, out_tok, total_tok, cost, latency_ms, 1 if success else 0),
            )
            await db.commit()
    except Exception:
        logger.exception("⚠️ record_gemini_call failed")


@router.get("/stats/gemini/summary", include_in_schema=False)
async def gemini_summary(range: str = "24h"):
    win = range_seconds(range)
    since = int(time.time()) - win if win is not None else 0

    async with db_connect(STATS_DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
            "COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0), COALESCE(AVG(latency_ms), 0), "
            "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) "
            "FROM gemini_calls WHERE ts >= ?",
            (since,),
        ) as cur:
            total = await cur.fetchone()
        async with db.execute(
            "SELECT endpoint, COUNT(*), COALESCE(SUM(total_tokens), 0), "
            "COALESCE(SUM(cost_usd), 0), COALESCE(AVG(latency_ms), 0) "
            "FROM gemini_calls WHERE ts >= ? GROUP BY endpoint ORDER BY SUM(cost_usd) DESC",
            (since,),
        ) as cur:
            by_endpoint = await cur.fetchall()
        async with db.execute(
            "SELECT model, COUNT(*), COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
            "FROM gemini_calls WHERE ts >= ? GROUP BY model",
            (since,),
        ) as cur:
            by_model = await cur.fetchall()

    # Aggregate query har doim 1 ta row qaytaradi (qiymatlari NULL bo'lishi mumkin).
    assert total is not None
    return {
        "range": range,
        "calls": int(total[0] or 0),
        "input_tokens": int(total[1] or 0),
        "output_tokens": int(total[2] or 0),
        "total_tokens": int(total[3] or 0),
        "cost_usd": round(float(total[4] or 0), 4),
        "avg_latency_ms": int(total[5] or 0),
        "errors": int(total[6] or 0),
        "by_endpoint": [
            {"endpoint": r[0], "calls": r[1], "total_tokens": r[2],
             "cost_usd": round(float(r[3]), 4), "avg_latency_ms": int(r[4])}
            for r in by_endpoint
        ],
        "by_model": [
            {"model": r[0], "calls": r[1], "total_tokens": r[2], "cost_usd": round(float(r[3]), 4)}
            for r in by_model
        ],
    }


@router.get("/stats/gemini/recent", include_in_schema=False)
async def gemini_recent(limit: int = 50):
    limit = max(1, min(int(limit or 50), 500))
    async with db_connect(STATS_DB_PATH) as db:
        async with db.execute(
            "SELECT id, ts, endpoint, model, input_tokens, output_tokens, total_tokens, "
            "cost_usd, latency_ms, success FROM gemini_calls ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return {
        "items": [
            {"id": r[0], "ts": r[1], "endpoint": r[2], "model": r[3],
             "input_tokens": r[4], "output_tokens": r[5], "total_tokens": r[6],
             "cost_usd": round(float(r[7] or 0), 4), "latency_ms": r[8], "success": bool(r[9])}
            for r in rows
        ]
    }


@router.get("/stats/gemini/timeseries", include_in_schema=False)
async def gemini_timeseries(range: str = "24h"):
    win = range_seconds(range) or 2592000
    bucket = 3600 if win <= 86400 else 86400
    since = int(time.time()) - win
    async with db_connect(STATS_DB_PATH) as db:
        async with db.execute(
            f"SELECT (ts / {bucket}) * {bucket} AS b, COUNT(*), "
            f"COALESCE(SUM(total_tokens), 0), COALESCE(SUM(cost_usd), 0) "
            f"FROM gemini_calls WHERE ts >= ? GROUP BY b ORDER BY b ASC",
            (since,),
        ) as cur:
            rows = await cur.fetchall()
    return {
        "range": range,
        "bucket_seconds": bucket,
        "series": [
            {"ts": r[0], "calls": r[1], "total_tokens": r[2], "cost_usd": round(float(r[3]), 4)}
            for r in rows
        ],
    }
