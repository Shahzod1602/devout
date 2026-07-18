"""SQLite (?-param) SQL → PostgreSQL ($N-param) tarjimon.

docs/POSTGRES_MIGRATION_PLAN.md — Phase 2 engine porti. Maqsad: 14 modulning raw
SQL'ini O'ZGARTIRMASDAN Postgres'da ishlatish. Dual-backend `db_connect`
(DB_BACKEND=postgres) so'rovni asyncpg'ga uzatishdan OLDIN shu tarjimondan
o'tkazadi. Faqat loyihada haqiqatan ishlatilgan naqshlar qamraladi (butun
umumiy SQL parser emas) — qamrov `test_sql_translate.py`da haqiqiy so'rovlar
bilan qotirilgan.
"""
import re

# Loyihada ishlatilgan SQLite-ga xos naqshlar:
_DATETIME_INTERVAL = re.compile(r"datetime\(\s*'now'\s*,\s*\?\s*\)", re.IGNORECASE)
_DATETIME_NOW = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_INSERT_OR_IGNORE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)


def _number_params(sql: str) -> str:
    """`?` pozitsion parametrlarni `$1, $2, ...`ga o'giradi (string-literal ichini tashlab).

    Loyiha so'rovlarida `?` faqat parametr sifatida keladi (literal ichida yo'q),
    lekin string-literal guard'i xavfsizlik uchun saqlanadi.
    """
    out = []
    n = 0
    in_str = False
    for c in sql:
        if c == "'":
            in_str = not in_str
            out.append(c)
        elif c == "?" and not in_str:
            n += 1
            out.append(f"${n}")
        else:
            out.append(c)
    return "".join(out)


def to_postgres(sql: str) -> tuple[str, bool]:
    """SQLite SQL → Postgres SQL. Qaytaradi: (tarjima, is_noop).

    is_noop=True (PRAGMA) — chaqiruvchi bajarmasdan jimgina o'tkazadi (Postgres'da
    PRAGMA yo'q; busy_timeout/wal_checkpoint/journal_mode kerak emas).
    """
    if sql.lstrip().upper().startswith("PRAGMA"):
        return "", True

    s = sql
    # datetime('now', ?)  →  (now() + (?)::text::interval)   — `?` saqlanadi, keyin raqamlanadi.
    #   Param SQLite'da '-60 days' (STRING) keladi. ::text bo'lmasa asyncpg $N'ni interval
    #   deb biladi va Python timedelta kutadi → DataError. ::text bilan string yuboriladi,
    #   PG o'zi interval'ga aylantiradi. Postgres interval '-60 days' = now()-60d. Mos.
    s = _DATETIME_INTERVAL.sub("(now() + (?)::text::interval)", s)
    # datetime('now')  →  now()
    s = _DATETIME_NOW.sub("now()", s)
    # INSERT OR IGNORE INTO ...  →  INSERT INTO ... ON CONFLICT DO NOTHING
    #   (target'siz ON CONFLICT — HAR QANDAY unique buzilishida DO NOTHING; SQLite
    #   OR IGNORE bilan bir xil semantika. UNIQUE indeks 0006 arbiter bo'ladi.)
    if _INSERT_OR_IGNORE.search(s):
        s = _INSERT_OR_IGNORE.sub("INSERT INTO", s)
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # ?  →  $1, $2, ...
    s = _number_params(s)
    return s, False
