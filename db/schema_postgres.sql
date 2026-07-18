-- PostgreSQL sxema (docs/POSTGRES_MIGRATION_PLAN.md, Phase 2).
-- SQLite migrations 0001-0006 ekvivalenti, bitta idempotent DDL.
-- Dizayn tamoyili: SQLite semantikasiga YAQIN saqlash — kod o'zgarmasin:
--   * accepted / permission flaglar INTEGER 0/1 (BOOLEAN emas) → SUM(accepted) va
--     bool(row[N]) o'zgarishsiz ishlaydi.
--   * ⚠️ Telegram driver_id/team_driver_id/message_id → BIGINT (int32'dan oshadi).
--   * file_blob → BYTEA. id → BIGINT IDENTITY (AUTOINCREMENT o'rniga).
--   * datetime('now') defaultlari → now() (TIMESTAMPTZ).

CREATE TABLE IF NOT EXISTS loads (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id       TEXT NOT NULL,
    load_id        TEXT NOT NULL,
    pickup_count   INTEGER NOT NULL DEFAULT 1,
    delivery_count INTEGER NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (group_id, load_id)
);

CREATE TABLE IF NOT EXISTS bols (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id   TEXT NOT NULL,
    load_id    TEXT NOT NULL,
    message_id BIGINT NOT NULL,
    file_blob  BYTEA NOT NULL,
    accepted   INTEGER NOT NULL DEFAULT 0,
    saved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pods (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id   TEXT NOT NULL,
    load_id    TEXT NOT NULL,
    message_id BIGINT NOT NULL,
    file_blob  BYTEA NOT NULL,
    saved_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bols_group_load ON bols(group_id, load_id);
CREATE INDEX IF NOT EXISTS idx_pods_group_load ON pods(group_id, load_id);
-- ⚠️ ON CONFLICT DO NOTHING arbiter'i (INSERT OR IGNORE ekvivalenti) — SHART.
CREATE UNIQUE INDEX IF NOT EXISTS idx_bols_unique ON bols(group_id, load_id, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pods_unique ON pods(group_id, load_id, message_id);

CREATE TABLE IF NOT EXISTS groups (
    group_id         TEXT PRIMARY KEY,
    driver_id        BIGINT,
    driver_name      TEXT,
    team_driver_id   BIGINT,
    team_driver_name TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS company_permissions (
    company_id              TEXT PRIMARY KEY,
    ticket_create           INTEGER NOT NULL DEFAULT 1,
    task_paraphrase         INTEGER NOT NULL DEFAULT 0,
    bol_pod_paperwork       INTEGER NOT NULL DEFAULT 1,
    check_in_check_out      INTEGER NOT NULL DEFAULT 1,
    sleep_time              INTEGER NOT NULL DEFAULT 1,
    photo_pdf               INTEGER NOT NULL DEFAULT 1,
    paperwork_driver_group  INTEGER NOT NULL DEFAULT 0,
    paperwork_internal_team INTEGER NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
