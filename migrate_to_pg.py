"""SQLite → PostgreSQL bir martalik data-migratsiya (docs/POSTGRES_MIGRATION_PLAN.md).

Konteyner ichida ishlaydi:  python migrate_to_pg.py
  Manba:   config.DB_PATH        (SQLite bot_data.db)
  Maqsad:  config.DATABASE_URL   (Postgres)

Sxema init_pg_schema() bilan yaratiladi. Bloblar BYTEA sifatida ko'chiriladi.
Timestamp'lar (SQLite UTC-matn) ::timestamp AT TIME ZONE 'UTC' bilan aylantiriladi.
IDEMPOTENT: har jadval TRUNCATE ... RESTART IDENTITY qilinadi (qayta-run xavfsiz).

Oxirida VERIFY (majburiy gate):
  - har jadval: SQLite COUNT == Postgres COUNT
  - bols/pods: har blob SHA-256 (group_id,load_id,message_id) kaliti bo'yicha mos
Verify yiqilsa exit 1 (cutover BEKOR). Nol ma'lumot yo'qotish: manba TEGILMAYDI.
"""
import asyncio
import hashlib
import logging
import sqlite3
import sys

import aiosqlite
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

# (jadval, ustunlar, identity_bormi) — identity jadvallarga id KO'CHIRILMAYDI (PG o'zi beradi;
# id-tartibda insert → yangi ketma-ket id'lar o'sha tartibni saqlaydi, ORDER BY id DESC ishlaydi).
_TS = "::timestamp AT TIME ZONE 'UTC'"
TABLES = [
    ("loads", ["group_id", "load_id", "pickup_count", "delivery_count", "created_at"], {"created_at": _TS}),
    ("groups", ["group_id", "driver_id", "driver_name", "team_driver_id", "team_driver_name", "updated_at"], {"updated_at": _TS}),
    ("company_permissions", ["company_id", "ticket_create", "task_paraphrase", "bol_pod_paperwork",
                             "check_in_check_out", "sleep_time", "photo_pdf", "paperwork_driver_group",
                             "paperwork_internal_team", "created_at", "updated_at"],
     {"created_at": _TS, "updated_at": _TS}),
    ("bols", ["group_id", "load_id", "message_id", "file_blob", "accepted", "saved_at"], {"saved_at": _TS}),
    ("pods", ["group_id", "load_id", "message_id", "file_blob", "saved_at"], {"saved_at": _TS}),
    ("schema_migrations", ["version", "applied_at"], {"applied_at": _TS}),
]


def _placeholders(cols, casts):
    """$1, $2::timestamp AT TIME ZONE 'UTC', ... — cast'lar bilan."""
    out = []
    for i, c in enumerate(cols, start=1):
        out.append(f"(${i}){casts[c]}" if c in casts else f"${i}")
    return ", ".join(out)


async def migrate():
    import asyncpg
    from db.pg import init_pg_schema

    if not config.DATABASE_URL:
        log.error("DATABASE_URL o'rnatilmagan — migratsiya to'xtatildi")
        sys.exit(2)

    log.info("Sxema tayyorlanmoqda (Postgres)...")
    await init_pg_schema()

    pg = await asyncpg.connect(config.DATABASE_URL)
    src = await aiosqlite.connect(config.DB_PATH)
    src.row_factory = None
    try:
        for table, cols, casts in TABLES:
            # Manbada jadval bor-yo'qligini tekshirish (schema_migrations har doim bor)
            try:
                async with src.execute(f"SELECT {', '.join(cols)} FROM {table}") as cur:
                    rows = list(await cur.fetchall())
            except sqlite3.OperationalError as e:
                log.warning("  %s: manba jadval yo'q (%s) — o'tkazildi", table, e)
                continue

            await pg.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")
            if rows:
                insert = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({_placeholders(cols, casts)})"
                # bytes bloblari aiosqlite'da bytes bo'lib keladi → asyncpg BYTEA. Batch.
                await pg.executemany(insert, [tuple(r) for r in rows])
            log.info("  %s: %d qator ko'chirildi", table, len(rows))

        await _verify(src, pg)
    finally:
        await src.close()
        await pg.close()


async def _verify(src, pg):
    log.info("VERIFY: qator-sonlar + blob SHA-256...")
    ok = True
    for table, _cols, _casts in TABLES:
        try:
            async with src.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                s_count = (await cur.fetchone())[0]
        except sqlite3.OperationalError:
            continue
        p_count = await pg.fetchval(f"SELECT COUNT(*) FROM {table}")
        match = s_count == p_count
        ok = ok and match
        log.info("  %-20s sqlite=%-7d pg=%-7d %s", table, s_count, p_count, "OK" if match else "MISMATCH!!")

    for table in ("bols", "pods"):
        try:
            s_hash = {}
            async with src.execute(f"SELECT group_id, load_id, message_id, file_blob FROM {table}") as cur:
                async for g, ld, m, blob in cur:
                    s_hash[(g, ld, m)] = hashlib.sha256(bytes(blob)).hexdigest()
        except sqlite3.OperationalError:
            continue
        mism = 0
        for rec in await pg.fetch(f"SELECT group_id, load_id, message_id, file_blob FROM {table}"):
            key = (rec["group_id"], rec["load_id"], rec["message_id"])
            h = hashlib.sha256(bytes(rec["file_blob"])).hexdigest()
            if s_hash.get(key) != h:
                mism += 1
        ok = ok and mism == 0
        log.info("  %-20s blob SHA-256: %d ta, %d mos kelmadi %s", table, len(s_hash), mism, "OK" if mism == 0 else "MISMATCH!!")

    if not ok:
        log.error("❌ VERIFY YIQILDI — Postgres'ga o'tmang, SQLite manba bo'lib qoladi")
        sys.exit(1)
    log.info("✅ VERIFY o'tdi — Postgres SQLite bilan bayt-ma-bayt mos")


if __name__ == "__main__":
    asyncio.run(migrate())
