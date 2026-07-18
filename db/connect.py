"""Shared aiosqlite connect helper (DBAUDIT-2).

busy_timeout per-connection runtime PRAGMA — faylda saqlanmaydi, shuning uchun
migrator'dagi bittasi yetmaydi. Har connection'da 5s kutamiz, darrov SQLITE_BUSY
bermaymiz.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
import config


@asynccontextmanager
async def db_connect(db_path: "Path | str") -> AsyncIterator[aiosqlite.Connection]:
    """Backend-aware ulanish.

    DB_BACKEND=postgres VA db_path asosiy DB_PATH bo'lsa → asyncpg pool (aiosqlite-mos
    adapter). Aks holda (SQLite yoki stats DB) → aiosqlite. Stats DB (STATS_DB_PATH)
    HAR DOIM SQLite'da qoladi (Phase 3 gacha) — shuning uchun path solishtiriladi.
    """
    if config.DB_BACKEND == "postgres" and str(db_path) == str(config.DB_PATH):
        from db.pg import _PgConn, get_pg_pool  # lazy — faqat postgres backend'da
        pool = await get_pg_pool()
        async with pool.acquire() as conn:
            # _PgConn aiosqlite.Connection interfeysini duck-type qiladi (execute/
            # commit/rollback + cursor) — statik tip mos emas, lekin runtime mos.
            yield _PgConn(conn)  # type: ignore[misc]
        return

    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA busy_timeout=5000")
        yield db
    finally:
        await db.close()
