-- Team driver qo'shildi (1 ta primary + 1 ta team).
-- Eski schema.py'da ALTER TABLE try/except qilingan edi — endi versioned migration.

ALTER TABLE groups ADD COLUMN team_driver_id INTEGER;
ALTER TABLE groups ADD COLUMN team_driver_name TEXT;
