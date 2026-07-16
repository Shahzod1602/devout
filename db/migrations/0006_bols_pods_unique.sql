-- audit v3 #22: bols/pods idempotentlik — bir xil (group_id, load_id, message_id)
-- dublikat yozuvlarni oldini oladi (double-tap Analyze / bir xabarni ikki marta ishlash).
-- Bugungi #13 (per-guruh lock + atomik callback pop) bilan birga defense-in-depth.
-- Avval mavjud dublikatlarni tozalaymiz (eng kichik id qoladi), keyin UNIQUE indeks
-- quramiz — aks holda dublikatli prod DB'da UNIQUE indeks yaratilmaydi.
DELETE FROM bols WHERE id NOT IN (SELECT MIN(id) FROM bols GROUP BY group_id, load_id, message_id);
DELETE FROM pods WHERE id NOT IN (SELECT MIN(id) FROM pods GROUP BY group_id, load_id, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bols_unique ON bols(group_id, load_id, message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pods_unique ON pods(group_id, load_id, message_id);
