"""PostgreSQL backend — asyncpg pool + aiosqlite-mos adapter (DB_BACKEND=postgres).

docs/POSTGRES_MIGRATION_PLAN.md Phase 2. db_connect postgres rejimida `_PgConn`
qaytaradi — u aiosqlite Connection interfeysini (execute → cursor; commit/rollback;
cursor.fetchone/fetchall/rowcount) taqlid qiladi, shuning uchun operations.py va
qolgan modullar O'ZGARMAYDI. Raw SQL `to_postgres()` bilan tarjima qilinadi
(?→$N, datetime('now')→now(), INSERT OR IGNORE→ON CONFLICT DO NOTHING, PRAGMA→noop).

MUHIM cheklov (dev/test v1): har `db.execute` autocommit (asyncpg default). Ko'p-
statement'li funksiyalar (clear_load_from_cache = 3 DELETE + commit) endi atomik
EMAS — lekin ular idempotent cleanup DELETE'lar, qisman xato juda kam va tiklanadi.
Kerak bo'lsa keyin `conn.transaction()` bilan atomiklashtiriladi.
"""
import logging
from pathlib import Path

import config

logger = logging.getLogger(__name__)

from db.sql_translate import to_postgres  # noqa: E402 — config'dan keyin, cycle yo'q

_SCHEMA_PG = Path(__file__).parent / "schema_postgres.sql"
_pool = None


async def get_pg_pool():
    """asyncpg pool (lazy, singleton). DATABASE_URL config'dan."""
    global _pool
    if _pool is None:
        import asyncpg  # lazy — faqat postgres backend'da kerak
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=12)
        logger.info("🐘 Postgres pool created (max_size=12)")
    return _pool


async def close_pg_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def init_pg_schema() -> None:
    """schema_postgres.sql'ni qo'llash (idempotent, CREATE ... IF NOT EXISTS)."""
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_PG.read_text())
    logger.info("🐘 Postgres schema ensured (%s)", _SCHEMA_PG.name)


class _PgCursor:
    """aiosqlite Cursor-mos: fetchone/fetchall (tuple) + rowcount (status'dan)."""

    def __init__(self, rows, status=""):
        self._rows = rows
        self._status = status
        self._idx = 0

    async def fetchone(self):
        if self._idx < len(self._rows):
            r = self._rows[self._idx]
            self._idx += 1
            return tuple(r)
        return None

    async def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return [tuple(r) for r in rows]

    @property
    def rowcount(self):
        # asyncpg status: 'INSERT 0 1' / 'UPDATE 2' / 'DELETE 3' — oxirgi son.
        if self._status:
            parts = self._status.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
        return -1


async def _run_query(conn, sql, params):
    pg_sql, noop = to_postgres(sql)
    if noop:  # PRAGMA — Postgres'da kerak emas
        return _PgCursor([], "")
    args = tuple(params or ())
    head = pg_sql.lstrip()[:6].upper()
    if head == "SELECT" or "RETURNING" in pg_sql.upper():
        rows = await conn.fetch(pg_sql, *args)
        return _PgCursor(rows, "")
    status = await conn.execute(pg_sql, *args)
    return _PgCursor([], status)


class _PgExecute:
    """`db.execute(sql, params)` natijasi — HAM awaitable, HAM async-CM (aiosqlite kabi).

    `cur = await db.execute(...)` (yozuv+rowcount) va
    `async with db.execute(...) as cur:` (o'qish) — ikkalasi ham ishlaydi.
    """

    def __init__(self, conn, sql, params):
        self._conn = conn
        self._sql = sql
        self._params = params

    def __await__(self):
        return _run_query(self._conn, self._sql, self._params).__await__()

    async def __aenter__(self):
        return await _run_query(self._conn, self._sql, self._params)

    async def __aexit__(self, *exc):
        return False


class _PgConn:
    """aiosqlite Connection-mos wrapper (asyncpg connection ustida)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _PgExecute(self._conn, sql, params)

    async def commit(self):
        pass  # asyncpg autocommit — no-op

    async def rollback(self):
        pass
