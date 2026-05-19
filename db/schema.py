"""bot_data.db schema initialization — delegates to versioned migrator."""
import logging

from config import DB_PATH

from .migrator import apply_migrations

logger = logging.getLogger(__name__)


async def init_db() -> None:
    """Apply pending schema migrations. Idempotent — safe at every startup."""
    await apply_migrations(DB_PATH)
    logger.info("✅ SQLite DB initialized: %s", DB_PATH)
