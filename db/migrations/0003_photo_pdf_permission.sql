-- photo_pdf permission qo'shildi (driver rasm yuborganda PDF qilish ruxsati).

ALTER TABLE company_permissions ADD COLUMN photo_pdf INTEGER NOT NULL DEFAULT 1;
