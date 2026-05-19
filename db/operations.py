"""Async CRUD on bot_data.db: loads, bols, pods, groups, company_permissions."""
import logging

import aiosqlite
from config import DB_PATH

logger = logging.getLogger(__name__)


# ====== loads ======

async def init_load_in_cache(group_id, load_id, pickup_count, delivery_count):
    """Load ni DB da yaratish (agar mavjud bo'lmasa)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO loads (group_id, load_id, pickup_count, delivery_count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(group_id, load_id) DO NOTHING""",
            (str(group_id), str(load_id), pickup_count, delivery_count),
        )
        await db.commit()
    logger.info("💾 Load %s initialized for group %s (pickups: %d, deliveries: %d)",
                load_id, group_id, pickup_count, delivery_count)


async def get_pickup_count(group_id, load_id) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pickup_count FROM loads WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1


async def get_delivery_count(group_id, load_id) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT delivery_count FROM loads WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1


async def get_load_from_cache(group_id, load_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pickup_count, delivery_count FROM loads WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"pickup_count": row[0], "delivery_count": row[1]}
    return None


async def clear_load_from_cache(group_id, load_id):
    """Load + barcha BOL/POD'larini DB dan o'chirish."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bols WHERE group_id=? AND load_id=?", (str(group_id), str(load_id)))
        await db.execute("DELETE FROM pods WHERE group_id=? AND load_id=?", (str(group_id), str(load_id)))
        await db.execute("DELETE FROM loads WHERE group_id=? AND load_id=?", (str(group_id), str(load_id)))
        await db.commit()
    logger.info("🗑️ Load %s cleared for group %s", load_id, group_id)


async def clear_all_loads_for_group(group_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bols WHERE group_id=?", (str(group_id),))
        await db.execute("DELETE FROM pods WHERE group_id=?", (str(group_id),))
        await db.execute("DELETE FROM loads WHERE group_id=?", (str(group_id),))
        await db.commit()
    logger.info("🗑️ All loads cleared for group %s", group_id)


# ====== bols ======

async def add_bol_to_cache(group_id, load_id, message_id, file_bytes: bytes):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bols (group_id, load_id, message_id, file_blob) VALUES (?, ?, ?, ?)",
            (str(group_id), str(load_id), message_id, file_bytes),
        )
        await db.commit()
    logger.info("💾 BOL added for group %s, load %s", group_id, load_id)


async def get_bols_count(group_id, load_id) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM bols WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_last_bol(group_id, load_id) -> bytes | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT file_blob FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return bytes(row[0]) if row else None


async def all_bols_accepted(group_id, load_id) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(accepted) FROM bols WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            count, accepted_sum = row if row else (0, 0)
            return count > 0 and (accepted_sum or 0) == count


async def is_bol_accepted(group_id, load_id) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT accepted FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False


async def set_last_bol_accepted(group_id, load_id, accepted=True):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE bols SET accepted=? WHERE id=?",
                (1 if accepted else 0, row[0]),
            )
            await db.commit()
            logger.info("✅ Last BOL accepted=%s for group %s, load %s", accepted, group_id, load_id)
            return True
    return False


async def set_bol_accepted(group_id, load_id, accepted=True):
    """Alias for set_last_bol_accepted (legacy)."""
    return await set_last_bol_accepted(group_id, load_id, accepted)


async def needs_more_bols(group_id, load_id) -> bool:
    count = await get_bols_count(group_id, load_id)
    pickup = await get_pickup_count(group_id, load_id)
    return count < pickup


async def has_bol_for_load(group_id, load_id) -> bool:
    return await get_bols_count(group_id, load_id) > 0


async def remove_last_bol_for_load(group_id, load_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM bols WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.execute("DELETE FROM bols WHERE id=?", (row[0],))
            await db.commit()
            logger.info("🗑️ Last BOL removed for group %s, load %s", group_id, load_id)
            return True
    return False


# ====== pods ======

async def add_pod_to_cache(group_id, load_id, message_id, file_bytes: bytes):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO pods (group_id, load_id, message_id, file_blob) VALUES (?, ?, ?, ?)",
            (str(group_id), str(load_id), message_id, file_bytes),
        )
        await db.commit()
    logger.info("💾 POD added for group %s, load %s", group_id, load_id)


async def get_pods_count(group_id, load_id) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM pods WHERE group_id=? AND load_id=?",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def can_accept_pod(group_id, load_id) -> bool:
    """POD qabul qilish mumkinmi (barcha BOL lar accepted bo'lishi kerak)."""
    count = await get_bols_count(group_id, load_id)
    pickup = await get_pickup_count(group_id, load_id)
    if count < pickup:
        return False
    return await all_bols_accepted(group_id, load_id)


async def has_pods_for_load(group_id, load_id) -> bool:
    return await get_pods_count(group_id, load_id) > 0


async def remove_last_pod_for_load(group_id, load_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM pods WHERE group_id=? AND load_id=? ORDER BY id DESC LIMIT 1",
            (str(group_id), str(load_id)),
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await db.execute("DELETE FROM pods WHERE id=?", (row[0],))
            await db.commit()
            logger.info("🗑️ Last POD removed for group %s, load %s", group_id, load_id)
            return True
    return False


# ====== groups (driver assignments) ======

async def save_driver_id_db(group_id, driver_id, driver_name):
    """Driver ID ni DB ga saqlash. Caller in-memory cache'ni alohida boshqaradi."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO groups (group_id, driver_id, driver_name, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(group_id) DO UPDATE SET
                driver_id   = excluded.driver_id,
                driver_name = excluded.driver_name,
                updated_at  = excluded.updated_at
        """, (str(group_id), driver_id, driver_name))
        await db.commit()


async def save_team_driver_id_db(group_id, driver_id, driver_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE groups SET team_driver_id=?, team_driver_name=?, updated_at=datetime('now') WHERE group_id=?",
            (driver_id, driver_name, str(group_id)),
        )
        await db.commit()


async def remove_team_driver_db(group_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE groups SET team_driver_id=NULL, team_driver_name=NULL, updated_at=datetime('now') WHERE group_id=?",
            (str(group_id),),
        )
        await db.commit()


# ====== company_permissions ======

async def get_company_permissions(company_id: str) -> dict | None:
    """company_id bo'yicha permissions olish."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ticket_create, task_paraphrase, bol_pod_paperwork, check_in_check_out, sleep_time, photo_pdf, created_at, updated_at "
            "FROM company_permissions WHERE company_id=?",
            (str(company_id),),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return {
                "companyId": int(company_id),
                "ticketCreate": bool(row[0]),
                "taskParaphrase": bool(row[1]),
                "bolPodPaperworkAnalysis": bool(row[2]),
                "checkInCheckOut": bool(row[3]),
                "sleepTime": bool(row[4]),
                "photoPdf": bool(row[5]),
                "createdAt": row[6],
                "updatedAt": row[7],
            }
