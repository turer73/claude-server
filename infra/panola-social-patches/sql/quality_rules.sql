-- PSOC-20260528 Task 02: quality_rules güçlendirme — HARD kurallar
-- Idempotent: INSERT OR IGNORE kullanılıyor
-- VPS: sqlite3 /opt/panola-social/data/social.db < quality_rules.sql

CREATE TABLE IF NOT EXISTS quality_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT UNIQUE NOT NULL,
    product TEXT,           -- NULL = tüm ürünler; 'kuafor' | 'petvet' | 'renderhane'
    rule_type TEXT NOT NULL, -- 'min_length' | 'max_length' | 'forbidden_pattern' | 'required_pattern' | 'min_count' | 'max_count'
    config TEXT NOT NULL,   -- JSON
    severity TEXT NOT NULL DEFAULT 'hard',  -- 'hard' = onayı engeller | 'soft' = uyarı
    active INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Global HARD kurallar
INSERT OR IGNORE INTO quality_rules (rule_id, product, rule_type, config, severity, description) VALUES
('min_text_length',   NULL, 'min_length',        '{"field":"content_text","min":80}',                                          'hard', 'İçerik metni en az 80 karakter'),
('max_text_length',   NULL, 'max_length',         '{"field":"content_text","max":2200}',                                        'hard', 'IG caption limiti (2200 karakter)'),
('no_placeholder',    NULL, 'forbidden_pattern',  '{"patterns":["\\[.*?\\]","\\{.*?\\}","TODO","PLACEHOLDER","lorem ipsum","undefined","null"]}', 'hard', 'Placeholder metin bırakılmamalı'),
('min_hashtags',      NULL, 'min_count',          '{"field":"hashtags","min":3}',                                               'hard', 'En az 3 hashtag'),
('max_hashtags',      NULL, 'max_count',          '{"field":"hashtags","max":30}',                                              'hard', 'IG hashtag limiti (30)'),
('no_duplicate_hash', NULL, 'forbidden_pattern',  '{"field":"hashtags","check":"unique"}',                                     'hard', 'Tekrarlanan hashtag olmamalı'),

-- Kuafor HARD kurallar
('kuafor_no_fake_salon',  'kuafor', 'forbidden_pattern', '{"patterns":["Salon Ad[ıi]","salon_adi","SALON_ADI","[A-Z] Salonu","[A-Z]{1,3} Saç","Örnek Salon","Test Salon","XYZ Salon","ABC Kuaför"]}', 'hard', 'Uydurma salon adı yasak'),
('kuafor_no_raw_price',   'kuafor', 'forbidden_pattern', '{"patterns":["\\d+\\s*TL","\\d+\\s*₺","fiyat\\s*\\d","ücret\\s*\\d"]}', 'hard', 'Ham fiyat yazılmamalı (müşteriye yanıltıcı)'),
('kuafor_no_guarantee',   'kuafor', 'forbidden_pattern', '{"patterns":["garantili","kesinlikle","100% sonuç","mutlaka düzelir"]}', 'hard', 'Garanti/kesinlik ifadesi yasak'),

-- Kuafor SOFT kurallar
('kuafor_samimi_tone',    'kuafor', 'required_pattern', '{"patterns":["[Ss]en|[Ss]iz|[Kk]endine|[Bb]ek[li]|[Gg]el|[Hh]ay[ai]l"]}', 'soft', 'Samimi/ikinci şahıs dil kullan'),
('kuafor_has_cta',        'kuafor', 'required_pattern', '{"patterns":["[Rr]andevu|[Ll]ink|[Pp]rofil|DM|[Yy]orum|[Ww]hatsApp|[Aa]ra"]}', 'soft', 'Randevu/CTA içermeli'),

-- PetVet HARD kurallar
('petvet_no_diagnosis',   'petvet', 'forbidden_pattern', '{"patterns":["teşhis","tedavi edin","ilaç kullanın","veterinere gitmeyin","evde tedavi"]}', 'hard', 'Tıbbi teşhis/tedavi tavsiyesi yasak'),
('petvet_no_fake_price',  'petvet', 'forbidden_pattern', '{"patterns":["\\d+\\s*TL","\\d+\\s*₺"]}', 'hard', 'Ham fiyat yazılmamalı'),

-- Renderhane SOFT kurallar
('renderhane_visual',     'renderhane', 'required_pattern', '{"patterns":["[Rr]ender|[Tt]asarım|[Mm]odel|3[Dd]|[Vv]izüel|[Bb]lender|[Mm]imari"]}', 'soft', 'Görsel/teknik odak olmalı');

-- Sonucu doğrula
SELECT COUNT(*) as kural_sayisi, severity FROM quality_rules GROUP BY severity;
