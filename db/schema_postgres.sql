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

-- Chat log — group chat orqali o'tgan HAR BIR xabarni yozadi (chat_logger.py
-- outer middleware), STARTED_GROUPS gate'idan mustaqil. /admin/chat UI'si shu
-- ustidan o'qiydi (real-time-messages/history).
CREATE TABLE IF NOT EXISTS chat_messages (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id             TEXT NOT NULL,
    message_id           BIGINT NOT NULL,
    user_id              BIGINT,
    user_name            TEXT,
    msg_type             TEXT NOT NULL DEFAULT 'text',
    text                 TEXT,
    reply_to_message_id  BIGINT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_group ON chat_messages(group_id, id);
-- Reply preview — Telegram javob berilganda asl xabarni to'liq beradi (msg.reply_to_message),
-- shuning uchun JOIN kerak emas: denormalized saqlaymiz (eski/hali loglanmagan xabarga
-- javob bo'lsa ham ishlaydi). Allaqachon deploy qilingan jadvalga ustun qo'shish — ADD
-- COLUMN IF NOT EXISTS idempotent, har startup'da xavfsiz qayta ishlaydi.
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS reply_user_name TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS reply_text TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS reply_msg_type TEXT;
-- Media fayllar (rasm/fayl/ovoz/video) — Telegram file_id'ni saqlaymiz, real vaqtda
-- /admin/chat/api/file/{id} orqali bot tokeni bilan Telegram'dan proksi qilib olib
-- beriladi (bloblarni bazaga yozib yubormaymiz — faqat file_id, arzon).
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS file_id TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS file_name TEXT;
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS file_size BIGINT;

CREATE TABLE IF NOT EXISTS chat_groups (
    group_id   TEXT PRIMARY KEY,
    title      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
