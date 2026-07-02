"""Versioned SQL migrator for bot_data.db.

Migrations: bot/db/migrations/NNNN_name.sql (4-raqamli prefiks, kamayuvchi tartibga
mos kelmasligi mumkin lekin tartib raqami muhim).

Qaysi migration'lar qo'llanganligi `schema_migrations` jadvalda saqlanadi.

Har bir migration fayli alohida statement'larga ajratilib, bittadan qo'llanadi.
Bu — bir faylda bir nechta `ALTER TABLE ... ADD COLUMN` bo'lganda, ulardan biri
allaqachon mavjud bo'lsa ("duplicate column"), faqat o'sha statement skip qilinib,
qolganlari baribir bajarilishini ta'minlaydi. (Avval butun fayl `executescript`
bilan bajarilardi — birinchi "duplicate column" butun skriptni to'xtatib,
migration "applied" deb belgilanib qolardi, keyingi ustunlar esa qo'shilmasdan
qolib ketardi.)
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


def _split_statements(sql: str) -> list[str]:
    """SQL skriptni alohida statement'larga ajratish (komment/bo'sh chunk'larsiz).

    Migration fayllar oddiy ';' bilan ajratilgan statement'lardan iborat —
    string literal ichida ';' yo'q, trigger yo'q — shu sabab oddiy split yetarli.
    """
    # Avval `--` komment qatorlarni olib tashlaymiz (ular ichida ';' bo'lishi
    # mumkin), keyin ';' bo'yicha ajratamiz — aks holda komment'dagi ';' statement'ni
    # noto'g'ri bo'lib yuboradi.
    no_comments = "\n".join(
        ln for ln in sql.splitlines() if not ln.strip().startswith("--")
    )
    return [s.strip() for s in no_comments.split(";") if s.strip()]


async def apply_migrations(db_path: Path | str) -> int:
    """Yangi migration'larni qo'llash. Returns: necha ta qo'llandi."""
    files = _discover_migrations()
    if not files:
        logger.warning("No migrations found in %s", MIGRATIONS_DIR)
        return 0

    async with aiosqlite.connect(db_path) as db:
        # DBS-6: boshqa connection qulflagan bo'lsa darrov SQLITE_BUSY bermay, 5s kutamiz.
        await db.execute("PRAGMA busy_timeout=5000")
        await _ensure_migrations_table(db)
        applied = await _applied_versions(db)

        new_count = 0
        for path in files:
            version = path.stem  # "0001_initial"
            if version in applied:
                continue
            logger.info("⬆️  Applying migration: %s", version)
            # Har statement'ni alohida bajaramiz va commit qilamiz. Alohida commit
            # PRAGMA (masalan journal_mode=WAL) transaction ichida bo'lib qolmasligi
            # uchun ham kerak.
            for stmt in _split_statements(path.read_text()):
                try:
                    await db.execute(stmt)
                    await db.commit()
                except aiosqlite.OperationalError as e:
                    # Idempotency: ustun/jadval allaqachon mavjud bo'lsa, faqat shu
                    # statement'ni skip qilamiz — qolganlari baribir bajariladi.
                    msg = str(e).lower()
                    if "duplicate column" in msg or "already exists" in msg:
                        logger.info("ℹ️  %s — statement skip (allaqachon mavjud): %s",
                                    version, " ".join(stmt.split())[:80])
                        await db.rollback()
                        continue
                    raise
            await db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            await db.commit()
            new_count += 1

    if new_count:
        logger.info("✅ Applied %d new migration(s)", new_count)
    else:
        logger.debug("Schema up-to-date (no new migrations)")
    return new_count
