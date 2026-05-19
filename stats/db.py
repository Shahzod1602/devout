"""Stats DB (bot_stats.db) — async aiosqlite connection + schema."""
import logging
from pathlib import Path

import aiosqlite

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATS_DB_PATH = DATA_DIR / "bot_stats.db"

logger = logging.getLogger(__name__)


async def init_stats_db() -> None:
    """Create all stats tables + WAL mode. Idempotent — safe at every startup."""
    async with aiosqlite.connect(STATS_DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS paperwork_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                group_id TEXT,
                result TEXT NOT NULL,
                load_id TEXT,
                latency_ms INTEGER DEFAULT 0,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pw_ts ON paperwork_events(ts);
            CREATE INDEX IF NOT EXISTS idx_pw_result ON paperwork_events(result);
            CREATE INDEX IF NOT EXISTS idx_pw_group ON paperwork_events(group_id);

            CREATE TABLE IF NOT EXISTS gemini_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                endpoint TEXT,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                success INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_gemini_ts ON gemini_calls(ts);
            CREATE INDEX IF NOT EXISTS idx_gemini_endpoint ON gemini_calls(endpoint);
            CREATE INDEX IF NOT EXISTS idx_gemini_model ON gemini_calls(model);

            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
        """)
        await db.commit()


RANGES = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000, "all": None}


def range_seconds(rng):
    """Parse human range string to seconds (None = all-time)."""
    return RANGES.get((rng or "24h").lower(), 86400)
