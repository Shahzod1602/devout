-- Fix: ba'zi eski prod DB'larda `team_driver_name` ustuni yetishmaydi.
-- Sabab: oldingi migrator 0002'dagi ikkita ALTER'ni bitta executescript bilan
-- bajarardi — `team_driver_id` allaqachon mavjud bo'lsa, birinchi ALTER
-- "duplicate column" berib butun skriptni to'xtatardi va `team_driver_name`
-- qo'shilmay qolardi. Natijada /teamdriver "no such column" bilan ishlamasdi.
-- Bu migration ustunni qayta qo'shadi; agar allaqachon mavjud bo'lsa, migrator
-- "duplicate column"ni xavfsiz skip qiladi.

ALTER TABLE groups ADD COLUMN team_driver_name TEXT;
