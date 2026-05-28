-- PSOC-20260528-v2 Task v2-02: product_knowledge kuafor enjeksiyonu
-- V1 yaklaşımı: 6 template MD → /opt/panola-social/prompts/kuafor/ (VPS'te dizin yok)
-- V2 yaklaşım: product_knowledge tablosuna tone+content_rules+topics enjeksiyonu
-- (VPS'te action-tipi product-agnostic prompts var, kuafor için ayrı dizin yok)
-- Idempotent: INSERT OR REPLACE + UNIQUE(product, key)
-- VPS: sqlite3 /opt/panola-social/data/social.db < product_knowledge_kuafor_v2.sql

CREATE TABLE IF NOT EXISTS product_knowledge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product    TEXT NOT NULL, -- 'kuafor' | 'petvet' | 'renderhane'
    key        TEXT NOT NULL, -- 'tone' | 'content_rules' | 'topics' | 'system_context'
    value      TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product, key)
);

-- Kuafor: Ton rehberi
INSERT OR REPLACE INTO product_knowledge (product, key, value) VALUES
('kuafor', 'tone',
'İkinci tekil veya çoğul şahıs kullan (sen/siz/kendinize). Arkadaşça ama profesyonel — aşırı selamlama yasak ("Merhaba güzel takipçiler" vb. jargon kullanma). Samimi ve kişisel ses tonu; okuyucu bir arkadaştan tavsiye alıyor gibi hissetmeli. Her içeriğin sonunda net bir CTA (randevu, link, DM, WhatsApp) zorunlu. Teşvik edici fiiller: "dene", "gel", "bak", "keşfet". Teknik terimler dengeli — müşteri anlayacak düzeyde açıkla.');

-- Kuafor: İçerik kuralları
INSERT OR REPLACE INTO product_knowledge (product, key, value) VALUES
('kuafor', 'content_rules',
'1. Salon adını asla uydurma — sadece {salon_adi} değişkenini kullan.
2. Ham fiyat (TL/₺/rakam+para birimi) asla yazma — "randevu alın" veya "fiyat bilgisi için ulaşın" kullan.
3. Tıbbi veya kimyasal garanti asla verme ("mutlaka düzelir", "kesinlikle", "100% sonuç" yasak).
4. Caption uzunluğu: minimum 80 karakter, maksimum 2200 karakter.
5. Hashtag sayısı: minimum 3, maksimum 30; tekrarlanan hashtag yasak.
6. İçerik gerçek hizmet/ipucu odaklı olmalı — muğlak veya genel içerik kabul edilmez.
7. Promosyon içeriğinde fayda vurgula, fiyat yerine sonucu anlat.');

-- Kuafor: Konu havuzu (6 temel kategori, V1 template başlıklarından)
INSERT OR REPLACE INTO product_knowledge (product, key, value) VALUES
('kuafor', 'topics',
'saç-bakım-ipuçları|trend-renk-tanıtım|hizmet-tanıtım|müşteri-dönüşüm|mevsimsel-içerik|ürün-önerisi');

-- Doğrula
SELECT product, key, length(value) as value_len FROM product_knowledge WHERE product = 'kuafor';
