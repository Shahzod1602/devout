-- chat_messages'ga reply preview ustunlari — schema_postgres.sql'dagi izohga qarang
-- (Telegram javob berilganda asl xabarni to'liq beradi, JOIN kerak emas).
ALTER TABLE chat_messages ADD COLUMN reply_user_name TEXT;
ALTER TABLE chat_messages ADD COLUMN reply_text TEXT;
ALTER TABLE chat_messages ADD COLUMN reply_msg_type TEXT;
