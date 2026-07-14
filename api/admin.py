"""Admin panel — auth bilan himoyalangan kuzatuv API'lari + UI.

Audit v3 H1 (authsiz control-plane) konteksti: bu router HAMMA ma'lumot
endpointlarini ADMIN_TOKEN ortiga yashiradi. Token o'rnatilmagan bo'lsa
panel butunlay o'chiq (503). Panel READ-ONLY — hech narsani o'zgartirmaydi.

Endpointlar (hammasi /admin ostida):
- GET  /admin            — UI (static/admin.html; login shell ochiq, data yo'q)
- POST /admin/api/login  — token tekshirib HttpOnly cookie o'rnatadi
- POST /admin/api/logout
- GET  /admin/api/overview | /state | /logs | /groups | /config
- GET  /admin/api/db/tables | /db/rows
"""
import asyncio
import hmac
import logging
import time
from collections import deque
from pathlib import Path

import log_buffer
import state as state_mod
from config import (
    ADMIN_TOKEN,
    BASE_URL,
    BOT_PORT,
    DB_PATH,
    ENV_LABEL,
    GEMINI_BOT_MODEL,
    PAGE_COUNT_ENFORCE,
    PAPERWORK_LOG_GROUP_ID,
    PO_MATCH_ENFORCE,
    VERTEX_LOCATION,
    VERTEX_PROJECT,
)
from db.connect import db_connect
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from stats import STATS_DB_PATH, checkin_recent, checkin_summary, checkin_timeseries

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", include_in_schema=False)

COOKIE_NAME = "admin_token"
COOKIE_MAX_AGE = 30 * 86400

# Login brute-force guard: IP -> (fail_count, lock_until_ts)
_login_fails: dict[str, tuple[int, float]] = {}


def _check_admin(request: Request) -> None:
    """Auth guard — cookie yoki X-Admin-Token header. Raise 503/401."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN o'rnatilmagan — panel o'chiq")
    supplied = request.headers.get("x-admin-token") or request.cookies.get(COOKIE_NAME) or ""
    if not hmac.compare_digest(supplied.encode(), ADMIN_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


class LoginRequest(BaseModel):
    token: str


@router.post("/api/login")
async def admin_login(body: LoginRequest, request: Request, response: Response):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN o'rnatilmagan — panel o'chiq")

    ip = request.client.host if request.client else "?"
    fails, lock_until = _login_fails.get(ip, (0, 0.0))
    if time.time() < lock_until:
        raise HTTPException(status_code=429, detail="Ko'p noto'g'ri urinish — 60s kuting")

    if not hmac.compare_digest(body.token.strip().encode(), ADMIN_TOKEN.encode()):
        fails += 1
        _login_fails[ip] = (fails, time.time() + 60 if fails >= 5 else 0.0)
        logger.warning("🔐 Admin login FAILED from %s (attempt %d)", ip, fails)
        raise HTTPException(status_code=401, detail="Noto'g'ri token")

    _login_fails.pop(ip, None)
    logger.info("🔐 Admin login OK from %s", ip)
    response.set_cookie(
        COOKIE_NAME, ADMIN_TOKEN, max_age=COOKIE_MAX_AGE,
        httponly=True, samesite="strict", path="/",
    )
    return {"ok": True}


@router.post("/api/logout")
async def admin_logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("")
@router.get("/")
async def admin_page():
    """UI shell — ma'lumot yo'q, hamma data auth ortida."""
    for candidate in (Path(__file__).parent.parent / "static" / "admin.html", Path("static/admin.html")):
        if candidate.exists():
            return FileResponse(candidate, media_type="text/html")
    raise HTTPException(status_code=404, detail="admin.html topilmadi")


# ====== Data endpoints ======

async def _count(db, sql: str, params=()) -> int:
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row[0] or 0) if row else 0


@router.get("/api/overview")
async def admin_overview(request: Request):
    _check_admin(request)
    now = time.time()

    async with db_connect(DB_PATH) as db:
        counts = {
            "groups": await _count(db, "SELECT COUNT(*) FROM groups"),
            "loads": await _count(db, "SELECT COUNT(*) FROM loads"),
            "bols": await _count(db, "SELECT COUNT(*) FROM bols"),
            "pods": await _count(db, "SELECT COUNT(*) FROM pods"),
            "companies": await _count(db, "SELECT COUNT(*) FROM company_permissions"),
        }

    day_ago = int(now) - 86400
    async with db_connect(STATS_DB_PATH) as sdb:
        today = {
            "paperwork_total": await _count(sdb, "SELECT COUNT(*) FROM paperwork_events WHERE ts >= ?", (day_ago,)),
            "paperwork_matched": await _count(
                sdb, "SELECT COUNT(*) FROM paperwork_events WHERE ts >= ? AND result='matched'", (day_ago,)),
            "checkin_total": await _count(sdb, "SELECT COUNT(*) FROM checkin_events WHERE ts >= ?", (day_ago,)),
            "checkin_sent": await _count(
                sdb, "SELECT COUNT(*) FROM checkin_events WHERE ts >= ? AND result='sent'", (day_ago,)),
            "gemini_calls": await _count(sdb, "SELECT COUNT(*) FROM gemini_calls WHERE ts >= ?", (day_ago,)),
        }
        async with sdb.execute("SELECT COALESCE(SUM(cost_usd),0) FROM gemini_calls WHERE ts >= ?", (day_ago,)) as cur:
            row = await cur.fetchone()
        gemini_cost = round(float(row[0] or 0), 4) if row else 0.0

    recent_logs = log_buffer.get_logs(limit=2000)
    hour_ago = now - 3600
    errors_1h = sum(1 for e in recent_logs if e["level"] in ("ERROR", "CRITICAL") and e["ts"] >= hour_ago)
    warns_1h = sum(1 for e in recent_logs if e["level"] == "WARNING" and e["ts"] >= hour_ago)
    last_log_age = round(now - recent_logs[-1]["ts"], 1) if recent_logs else None

    return {
        "env": ENV_LABEL,
        "now": now,
        "uptime_s": int(now - state_mod.STARTED_AT),
        "port": BOT_PORT,
        "backend": BASE_URL,
        "queues": {
            "message_queue": state_mod.message_queue.qsize(),
            "message_queue_max": 2000,
            "failed_messages": len(state_mod.FAILED_MESSAGES_QUEUE),
            "history_dedup_keys": len(state_mod.HISTORY_SENT_MESSAGE_KEYS),
        },
        "counts": counts,
        "today": {**today, "gemini_cost_usd": gemini_cost},
        "health": {
            "errors_1h": errors_1h,
            "warnings_1h": warns_1h,
            "last_log_age_s": last_log_age,
            "asyncio_tasks": len(asyncio.all_tasks()),
        },
        "providers": {
            "openai": state_mod.openai_client is not None,
            "groq": state_mod.groq_client is not None,
            "cerebras": state_mod.cerebras_client is not None,
            "gemini_model": GEMINI_BOT_MODEL,
            "vertex_project": (VERTEX_PROJECT[:4] + "…" + VERTEX_PROJECT[-4:]) if VERTEX_PROJECT else None,
            "vertex_location": VERTEX_LOCATION,
        },
        "flags": {
            "PO_MATCH_ENFORCE": PO_MATCH_ENFORCE,
            "PAGE_COUNT_ENFORCE": PAGE_COUNT_ENFORCE,
            "PAPERWORK_LOG_GROUP": bool(PAPERWORK_LOG_GROUP_ID),
        },
    }


@router.get("/api/logs")
async def admin_logs(request: Request, after_id: int = 0, level: str | None = None,
                     q: str | None = None, limit: int = 300):
    _check_admin(request)
    limit = max(1, min(int(limit or 300), 1000))
    return {"items": log_buffer.get_logs(after_id=after_id, level=level, q=q, limit=limit)}


def _sample(value, n=20):
    """State strukturasidan xavfsiz namuna — JSON'ga sig'adigan ko'rinishda."""
    try:
        if isinstance(value, dict):
            items = list(value.items())[:n]
            return {str(k): repr(v)[:200] for k, v in items}
        if isinstance(value, (set, frozenset, list, tuple, deque)):
            return [repr(v)[:200] for v in list(value)[:n]]
    except Exception:  # noqa: BLE001 — introspektsiya hech qachon panelni yiqitmasin
        return None
    return None


@router.get("/api/state")
async def admin_state(request: Request):
    _check_admin(request)
    out = []
    for name in sorted(dir(state_mod)):
        if name.startswith("_"):
            continue
        value = getattr(state_mod, name)
        if isinstance(value, asyncio.Queue):
            out.append({"name": name, "type": "Queue", "size": value.qsize(), "sample": None})
        elif isinstance(value, (dict, set, frozenset, list, deque)):
            out.append({"name": name, "type": type(value).__name__, "size": len(value), "sample": _sample(value)})
    return {"structures": out}


@router.get("/api/groups")
async def admin_groups(request: Request):
    _check_admin(request)
    async with db_connect(DB_PATH) as db:
        async with db.execute(
            "SELECT group_id, driver_id, driver_name, team_driver_id, team_driver_name, updated_at FROM groups"
        ) as cur:
            rows = await cur.fetchall()
        async with db.execute(
            "SELECT group_id, COUNT(DISTINCT load_id) FROM loads GROUP BY group_id"
        ) as cur:
            load_counts: dict[str, int] = {r[0]: r[1] for r in await cur.fetchall()}

    now = time.time()

    def _variants(gid):
        out = [gid, str(gid)]
        if str(gid).lstrip("-").isdigit():
            out.append(int(gid))
        return out

    def _lookup(container, gid, default=None):
        for v in _variants(gid):
            try:
                if isinstance(container, dict) and v in container:
                    return container[v]
                if not isinstance(container, dict) and v in container:
                    return True
            except TypeError:
                continue
        return default

    groups = []
    for gid, drv_id, drv_name, team_id, team_name, updated_at in rows:
        cd = _lookup(state_mod.DRIVER_COOLDOWN, gid)
        pending = _lookup(state_mod.GROUP_PENDING_IMAGES, gid) or []
        groups.append({
            "group_id": gid,
            "driver_id": drv_id, "driver_name": drv_name,
            "team_driver_id": team_id, "team_driver_name": team_name,
            "updated_at": updated_at,
            "loads_cached": load_counts.get(gid, 0),
            "started": bool(_lookup(state_mod.STARTED_GROUPS, gid, False)),
            "awaiting_token": _lookup(state_mod.AWAITING_TOKEN, gid) is not None,
            "pending_images": len(pending) if hasattr(pending, "__len__") else 0,
            "ticket_status": _lookup(state_mod.GROUP_TICKET_STATUS, gid),
            "last_msg_age_s": round(now - cd, 1) if isinstance(cd, (int, float)) else None,
        })
    groups.sort(key=lambda g: str(g["updated_at"] or ""), reverse=True)
    return {"groups": groups, "total": len(groups)}


# ====== Checkin stats (auth ostida — stats.checkin router'siz) ======

@router.get("/api/checkin/summary")
async def admin_checkin_summary(request: Request, range: str = "24h"):
    _check_admin(request)
    return await checkin_summary(range)


@router.get("/api/checkin/timeseries")
async def admin_checkin_timeseries(request: Request, range: str = "7d"):
    _check_admin(request)
    return await checkin_timeseries(range)


@router.get("/api/checkin/recent")
async def admin_checkin_recent(request: Request, limit: int = 50):
    _check_admin(request)
    return await checkin_recent(limit)


# ====== DB browser (read-only) ======

_DBS = {"main": DB_PATH, "stats": STATS_DB_PATH}


async def _table_names(db) -> list[str]:
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ) as cur:
        return [r[0] for r in await cur.fetchall()]


@router.get("/api/db/tables")
async def admin_db_tables(request: Request):
    _check_admin(request)
    out = []
    for db_key, path in _DBS.items():
        try:
            async with db_connect(path) as db:
                for tbl in await _table_names(db):
                    out.append({
                        "db": db_key, "table": tbl,
                        "rows": await _count(db, f'SELECT COUNT(*) FROM "{tbl}"'),
                    })
        except Exception as e:  # noqa: BLE001 — DB fayli hali yaratilmagan bo'lishi mumkin
            out.append({"db": db_key, "table": f"(xato: {e})", "rows": 0})
    return {"tables": out}


@router.get("/api/db/rows")
async def admin_db_rows(request: Request, db: str = "main", table: str = "",
                        limit: int = 50, offset: int = 0, search: str = ""):
    _check_admin(request)
    path = _DBS.get(db)
    if path is None:
        raise HTTPException(status_code=422, detail="db main yoki stats bo'lishi kerak")
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    async with db_connect(path) as conn:
        tables = await _table_names(conn)
        if table not in tables:
            raise HTTPException(status_code=404, detail=f"Jadval topilmadi: {table!r}")

        async with conn.execute(f'PRAGMA table_info("{table}")') as cur:
            cols = [(r[1], (r[2] or "").upper()) for r in await cur.fetchall()]
        col_names = [c[0] for c in cols]

        # BLOB ustunlarini SELECT'da o'lchamga almashtiramiz — MB'lab fayl bytes
        # JSON'ga oqib ketmasin.
        select_parts = [
            f'length("{name}") || \' B blob\' AS "{name}"' if "BLOB" in ctype else f'"{name}"'
            for name, ctype in cols
        ]

        where, params = "", []
        if search.strip():
            like = f"%{search.strip()}%"
            where = " WHERE " + " OR ".join(f'CAST("{c}" AS TEXT) LIKE ?' for c, t in cols if "BLOB" not in t)
            params = [like] * sum(1 for _, t in cols if "BLOB" not in t)

        total = await _count(conn, f'SELECT COUNT(*) FROM "{table}"{where}', tuple(params))
        async with conn.execute(
            f'SELECT {", ".join(select_parts)} FROM "{table}"{where} '
            f'ORDER BY rowid DESC LIMIT ? OFFSET ?',
            (*params, limit, offset),
        ) as cur:
            rows = await cur.fetchall()

    def cell(v):
        if isinstance(v, bytes):
            return f"{len(v)} B blob"
        s = str(v) if v is not None else None
        return s[:300] + "…" if s and len(s) > 300 else s

    return {
        "db": db, "table": table, "columns": col_names,
        "rows": [[cell(v) for v in r] for r in rows],
        "total": total, "limit": limit, "offset": offset,
    }
