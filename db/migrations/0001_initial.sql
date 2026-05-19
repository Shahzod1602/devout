-- Initial schema: loads / bols / pods / groups / company_permissions
-- Re-runnable: barcha CREATE'lar IF NOT EXISTS bilan (init_db idempotent edi).

CREATE TABLE IF NOT EXISTS loads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id       TEXT NOT NULL,
    load_id        TEXT NOT NULL,
    pickup_count   INTEGER NOT NULL DEFAULT 1,
    delivery_count INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(group_id, load_id)
);

CREATE TABLE IF NOT EXISTS bols (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL,
    load_id    TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    file_blob  BLOB NOT NULL,
    accepted   INTEGER NOT NULL DEFAULT 0,
    saved_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL,
    load_id    TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    file_blob  BLOB NOT NULL,
    saved_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bols_group_load ON bols(group_id, load_id);
CREATE INDEX IF NOT EXISTS idx_pods_group_load ON pods(group_id, load_id);

CREATE TABLE IF NOT EXISTS groups (
    group_id    TEXT PRIMARY KEY,
    driver_id   INTEGER,
    driver_name TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS company_permissions (
    company_id          TEXT PRIMARY KEY,
    ticket_create       INTEGER NOT NULL DEFAULT 1,
    task_paraphrase     INTEGER NOT NULL DEFAULT 0,
    bol_pod_paperwork   INTEGER NOT NULL DEFAULT 1,
    check_in_check_out  INTEGER NOT NULL DEFAULT 1,
    sleep_time          INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
