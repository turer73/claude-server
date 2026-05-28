-- PSOC-20260528-v2 Task v2-01: quality_rules güçlendirme
-- Şema: VPS gerçek şema (id, product, rule_type, rule, severity)
-- V1 farkı: rule_id/config/active/description kaldırıldı; config -> rule
-- Idempotent: INSERT OR REPLACE + explicit id
-- VPS: sqlite3 /opt/panola-social/data/social.db < quality_rules_v2.sql

CREATE TABLE IF NOT EXISTS quality_rules (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    product   TEXT,          -- NULL = tüm ürünler; 'kuafor' | 'petvet' | 'renderhane'
    rule_type TEXT NOT NULL, -- 'min_length' | 'max_length' | 'forbidden_pattern' | 'required_pattern' | 'min_count' | 'max_count'
    rule      TEXT NOT NULL, -- JSON konfigürasyon
    severity  TEXT NOT NULL DEFAULT 'hard'  -- 'hard' = onayı engeller | 'soft' = uyarı
);

-- Global HARD kurallar (id: 1-6)
INSERT OR REPLACE INTO quality_rules (id, product, rule_type, rule, severity) VALUES
(1,  NULL, 'min_length',        '{"field":"content_text","min":80}',
     'hard'),
(2,  NULL, 'max_length',        '{"field":"content_text","max":2200}',
     'hard'),
(3,  NULL, 'forbidden_pattern', '{"patterns":["\\[.*?\\]","\\{.*?\\}","TODO","PLACEHOLDER","lorem ipsum","undefined","null"]}',
     'hard'),
(4,  NULL, 'min_count',         '{"field":"hashtags","min":3}',
     'hard'),
(5,  NULL, 'max_count',         '{"field":"hashtags","max":30}',
     'hard'),
(6,  NULL, 'forbidden_pattern', '{"field":"hashtags","check":"unique"}',
     'hard');

-- Kuafor HARD kurallar (id: 11-13)
INSERT OR REPLACE INTO quality_rules (id, product, rule_type, rule, severity) VALUES
(11, 'kuafor', 'forbidden_pattern',
     '{"patterns":["Salon Ad[ıi]","salon_adi","SALON_ADI","Örnek Salon","Test Salon","XYZ Salon","ABC Kuaför"]}',
     'hard'),
(12, 'kuafor', 'forbidden_pattern',
     '{"patterns":["[0-9]+\\s*TL","[0-9]+\\s*₺","fiyat\\s*[0-9]","ücret\\s*[0-9]"]}',
     'hard'),
(13, 'kuafor', 'forbidden_pattern',
     '{"patterns":["garantili","kesinlikle","100% sonuç","mutlaka düzelir"]}',
     'hard');

-- Kuafor SOFT kurallar (id: 14-15)
INSERT OR REPLACE INTO quality_rules (id, product, rule_type, rule, severity) VALUES
(14, 'kuafor', 'required_pattern',
     '{"patterns":["[Ss]en|[Ss]iz|[Kk]endine|[Bb]ek[li]|[Gg]el|[Hh]ay[ai]l"]}',
     'soft'),
(15, 'kuafor', 'required_pattern',
     '{"patterns":["[Rr]andevu|[Ll]ink|[Pp]rofil|DM|[Yy]orum|[Ww]hats[Aa]pp|[Aa]ra"]}',
     'soft');

-- PetVet HARD kurallar (id: 21-22)
INSERT OR REPLACE INTO quality_rules (id, product, rule_type, rule, severity) VALUES
(21, 'petvet', 'forbidden_pattern',
     '{"patterns":["teşhis","tedavi edin","ilaç kullanın","veterinere gitmeyin","evde tedavi"]}',
     'hard'),
(22, 'petvet', 'forbidden_pattern',
     '{"patterns":["[0-9]+\\s*TL","[0-9]+\\s*₺"]}',
     'hard');

-- Renderhane SOFT kural (id: 31)
INSERT OR REPLACE INTO quality_rules (id, product, rule_type, rule, severity) VALUES
(31, 'renderhane', 'required_pattern',
     '{"patterns":["[Rr]ender|[Tt]asarım|[Mm]odel|3[Dd]|[Vv]izüel|[Bb]lender|[Mm]imari"]}',
     'soft');

-- Doğrula
SELECT COUNT(*) as kural_sayisi, severity FROM quality_rules GROUP BY severity;
