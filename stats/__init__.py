"""Bot stats package — paperwork events and Gemini API cost tracking.

Public API:
    init_stats_db()                  — call once at startup
    record_paperwork_event(...)      — record one paperwork analyze attempt
    record_gemini_call(...)          — record one Gemini API call
    current_gemini_endpoint          — ContextVar tagging the current endpoint
    paperwork_router, gemini_router  — FastAPI routers to include in the app
"""
from .db import STATS_DB_PATH, init_stats_db
from .gemini import (
    GEMINI_PRICING_PER_1M,
    record_gemini_call,
)
from .gemini import (
    current_endpoint as current_gemini_endpoint,
)
from .gemini import (
    router as gemini_router,
)
from .paperwork import record_paperwork_event
from .paperwork import router as paperwork_router

__all__ = [
    "STATS_DB_PATH",
    "init_stats_db",
    "record_paperwork_event",
    "record_gemini_call",
    "current_gemini_endpoint",
    "GEMINI_PRICING_PER_1M",
    "paperwork_router",
    "gemini_router",
]
