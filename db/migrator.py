"""Versioned SQL migrator for bot_data.db.

Migrations: bot/db/migrations/NNNN_name.sql (4-raqamli prefiks, kamayuvchi tartibga
mos kelmasligi mumkin lekin tartib raqami muhim).

Qaysi migration'lar qo'llanganligi `schema_migrations` jadvalda saqlanadi.
"""
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def _ensure_migrations_table(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    await db.commit()


async def _applied_versions(db: aiosqlite.Connection) -> set[str]:
    async with db.execute("SELECT version FROM schema_migrations") as cur:
        rows = await cur.fetchall()
    return {row[0] for row in rows}


def _discover_migrations() -> list[Path]:
    """List .sql files sortlangan tartibda (NNNN_name.sql)."""
    if not MIGRATIONS_DIR.exists():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


async def apply_migrations(db_path: Path | str) -> int:
    """Yangi migration'larni qo'llash. Returns: necha ta qo'llandi."""
    files = _discover_migrations()
    if not files:
        logger.warning("No migrations found in %s", MIGRATIONS_DIR)
        return 0

    async with aiosqlite.connect(db_path) as db:
        await _ensure_migrations_table(db)
        applied = await _applied_versions(db)

        new_count = 0
        for path in files:
            version = path.stem  # "0001_initial"
            if version in applied:
                continue
            sql = path.read_text()
            logger.info("⬆️  Applying migration: %s", version)
            try:
                await db.executescript(sql)
                await db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                await db.commit()
                new_count += 1
            except aiosqlite.OperationalError as e:
                # SQLite ALTER TABLE — agar ustun mavjud bo'lsa, "duplicate column" beradi.
                # Idempotency: avval bu kod ALTER + try/except bo'lgan, shu yerda
                # ham bardosh berishimiz kerak.
                if "duplicate column" in str(e).lower():
                    logger.info("ℹ️  Migration %s — schema allaqachon mavjud, skip", version)
                    await db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                    await db.commit()
                    new_count += 1
                else:
                    raise

    if new_count:
        logger.info("✅ Applied %d new migration(s)", new_count)
    else:
        logger.debug("Schema up-to-date (no new migrations)")
    return new_count
