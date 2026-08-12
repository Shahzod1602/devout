-- chat_messages'ga media fayl ustunlari — schema_postgres.sql'dagi izohga qarang
-- (bazaga blob emas, faqat Telegram file_id — /admin/chat/api/file/{id} proksi qiladi).
ALTER TABLE chat_messages ADD COLUMN file_id TEXT;
ALTER TABLE chat_messages ADD COLUMN file_name TEXT;
ALTER TABLE chat_messages ADD COLUMN file_size INTEGER;
