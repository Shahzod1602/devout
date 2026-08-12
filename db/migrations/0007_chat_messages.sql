-- Chat log — group chat orqali o'tgan HAR BIR xabarni yozadi (chat_logger.py
-- outer middleware), STARTED_GROUPS gate'idan mustaqil. SQLite ekvivalenti
-- (schema_postgres.sql'dagi bir xil jadval, dev/rollback fallback uchun).

CREATE TABLE IF NOT EXISTS chat_messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id             TEXT NOT NULL,
    message_id           INTEGER NOT NULL,
    user_id              INTEGER,
    user_name            TEXT,
    msg_type             TEXT NOT NULL DEFAULT 'text',
    text                 TEXT,
    reply_to_message_id  INTEGER,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_group ON chat_messages(group_id, id);

CREATE TABLE IF NOT EXISTS chat_groups (
    group_id   TEXT PRIMARY KEY,
    title      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
