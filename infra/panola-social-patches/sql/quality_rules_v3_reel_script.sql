-- PSOC-20260529-02 V3 REVIZE: reel_script quality_rules (B uyumlu)
-- Gercek VPS semasi: (id, product, rule_type, rule, severity)
-- Not giris semasi (content_type, rule_key, severity, rule_text, penalty, active) bu tabloya uymuyor;
--   adapte edildi: content_type='reel_script' JSON icinde "content_type" marker, severity uppercase->lowercase
--   INFO -> 'soft' (tablo 'hard'|'soft' enum destekliyor)
--   reel_byte_budget: V3-oncesi 2 satirdi (min_length+max_length), 5-satir hedefi icin range_check'e birlestirme.
-- caption alani V2 ile ayni (V3 revize B karari — downstream opak, alan adi korundu).
-- Deploy: scripts/vps-run.sh "sqlite3 /opt/panola-social/data/social.db" < bu-dosya
-- Oncesi backup: scripts/vps-run.sh "cp /opt/panola-social/data/social.db /opt/panola-social/data/social.db.bak-pre-v3-$(date +%Y%m%d-%H%M%S)"

SELECT 'before' as phase, COUNT(*) as kural_sayisi FROM quality_rules;

-- 5 yeni reel_script kurali (auto-inc id — v2-01'den ders)
INSERT INTO quality_rules (product, rule_type, rule, severity) VALUES

-- 1. Spesifik istatistik/oran yasagi (HARD)
('kuafor', 'forbidden_pattern',
 '{"content_type":"reel_script","field":"caption","patterns":["\\d+\\s*%","\\d+\\s+(musteri|randevu|gelir|kisi|saat|gun)"],"description":"reel_no_specific_stats: caption icinde istatistik/oran yasak; penalty=999 -> otomatik fail"}',
 'hard'),

-- 2. Byte butcesi: tek aralik kurali (< 700 veya > 1000 fail) (SOFT)
('kuafor', 'range_check',
 '{"content_type":"reel_script","field":"byte_sayisi","min":700,"max":1000,"fallback":"len(caption.encode(utf-8))","description":"reel_byte_budget: byte_sayisi 700-1000 olmali, yoksa caption UTF-8 byte uzunlugu kullan"}',
 'soft'),

-- 3. Emoji siniri: caption + hashtags + scenes[*].emoji toplami <= 3 (SOFT)
('kuafor', 'max_count',
 '{"content_type":"reel_script","fields":["caption","hashtags","scenes.emoji"],"max":3,"description":"reel_emoji_cap: toplam emoji <= 3"}',
 'soft'),

-- 4. Dogal ton: yasak reklam dili (SOFT)
('kuafor', 'forbidden_pattern',
 '{"content_type":"reel_script","field":"caption","patterns":["Profesyonel tavsiye","Uzman gorusu","Garantili","Kesin sonuc","%100 sonuc"],"description":"reel_natural_tone: reklam/garanti dili yasak"}',
 'soft'),

-- 5. Birinci sahis: scenes[*].anlatici_metni 2. tekil emir kipi yasak (INFO -> soft)
('kuafor', 'forbidden_pattern',
 '{"content_type":"reel_script","field":"scenes.anlatici_metni","patterns":["(^|\\s)(Sen|sen)\\s+(yap|et|dene|al|ver|kullan)"],"description":"reel_first_person: 2. tekil emir kipi scenes anlatici metninde yasak"}',
 'soft');

SELECT 'after' as phase, COUNT(*) as kural_sayisi, severity FROM quality_rules GROUP BY severity;
