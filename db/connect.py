"""Shared aiosqlite connect helper (DBAUDIT-2).

busy_timeout per-connection runtime PRAGMA — faylda saqlanmaydi, shuning uchun
migrator'dagi bittasi yetmaydi. Har connection'da 5s kutamiz, darrov SQLITE_BUSY
bermaymiz.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite


@asynccontextmanager
async def db_connect(db_path: "Path | str") -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA busy_timeout=5000")
        yield db
    finally:
        await db.close()
