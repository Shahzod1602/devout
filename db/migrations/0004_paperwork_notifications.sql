-- Paperwork issue notification toggles qo'shildi (Driver Group / Internal Team).
-- Backend paperwork-issue yaratganda bot bu toggle'larga qarab guruh(lar)ga inline
-- tugmali xabar yuboradi. Default 0 (off) — mavjud company'lar uchun kutilmagan
-- xabar bo'lmasin.

ALTER TABLE company_permissions ADD COLUMN paperwork_driver_group INTEGER NOT NULL DEFAULT 0;
ALTER TABLE company_permissions ADD COLUMN paperwork_internal_team INTEGER NOT NULL DEFAULT 0;
