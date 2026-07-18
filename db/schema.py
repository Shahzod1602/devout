"""bot_data.db schema initialization — delegates to versioned migrator (SQLite)
yoki schema_postgres.sql (DB_BACKEND=postgres)."""
import logging

import config
from config import DB_PATH

from .migrator import apply_migrations

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Sxemani tayyorlash. Idempotent — har startupda xavfsiz.

    DB_BACKEND=postgres → schema_postgres.sql; aks holda SQLite migratsiyalari.
    #19 blob-TTL tozalash BU YERDA emas — u main.py'da FON task sifatida ishlaydi.
    """
    if config.DB_BACKEND == "postgres":
        from db.pg import init_pg_schema  # lazy
        await init_pg_schema()
        logger.info("✅ Postgres DB initialized (DB_BACKEND=postgres)")
        return
    await apply_migrations(DB_PATH)
    logger.info("✅ SQLite DB initialized: %s", DB_PATH)
