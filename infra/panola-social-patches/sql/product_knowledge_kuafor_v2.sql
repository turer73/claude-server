-- Panola Social — product_knowledge kuafor tone+content_rules enjeksiyonu
-- Tarih: 2026-05-28
--
-- GERÇEK SCHEMA (note #99555):
--   id INTEGER PRIMARY KEY AUTOINCREMENT
--   product TEXT NOT NULL
--   category TEXT NOT NULL  (örn: 'identity', 'features', 'limitations', 'tone', 'content_rules')
--   key TEXT NOT NULL
--   value TEXT NOT NULL
--   UNIQUE(product, category, key)
--
-- STRATEJI: Template'leri product-spesifik dosyaya kopyalamak YERINE
-- mevcut product-agnostic template'ler product_knowledge'dan tone+kurallari okuyor.
-- Kuafor'a samimi/jargon ton ve uydurma adi yasaklarini buradan enjekte.

.headers on
.mode column

-- 1. Mevcut kuafor knowledge audit
SELECT 'BEFORE — kuafor knowledge:' AS '';
SELECT category, key, substr(value, 1, 60) AS value_preview
  FROM product_knowledge
 WHERE product='kuafor'
 ORDER BY category, key;

-- 2. Kuafor tone kayıtları (UNIQUE constraint nedeniyle ON CONFLICT)
INSERT INTO product_knowledge (product, category, key, value) VALUES
  ('kuafor', 'tone', 'voice', 'Samimi, enerjik, esnaf abi-abla agzindan. Salon sahibi/usta gibi konus, ofis dili YASAK.'),
  ('kuafor', 'tone', 'forbidden_phrases', 'verimliligi artirin, optimize edilmis, etkin yonetim, kullanici dostu, surec optimizasyonu'),
  ('kuafor', 'tone', 'approved_jargon', 'no-show, set basi, rebooking, kasa kapanisi, vardiya cakismasi, musteri sirkulasyonu, fon kalabaligi, bayram yogunlugu, dugun sezonu, tezgah, dahili gun'),
  ('kuafor', 'tone', 'sentence_style', 'Cumleler kisa, gunluk dilde. Aktif cumle. Markdown bold (**) YASAK. Max 3 emoji.'),
  ('kuafor', 'tone', 'audience', 'Turkiye esnaf segment kuafor/berber/barbershop/guzellik salonu sahipleri 25-50 yas mobile-first kullanim')
ON CONFLICT(product, category, key) DO UPDATE SET value=excluded.value, created_at=datetime('now');

-- 3. Kuafor content_rules (uydurma yasak)
INSERT INTO product_knowledge (product, category, key, value) VALUES
  ('kuafor', 'content_rules', 'no_fake_business_names', 'YASAK: salon adi uydurma (Salon Ayse, Hair Studio vb.)'),
  ('kuafor', 'content_rules', 'no_fake_person_names', 'YASAK: kisi adi uydurma (Ayse Hanim, Mehmet Usta vb.)'),
  ('kuafor', 'content_rules', 'no_specific_neighborhoods', 'YASAK: spesifik mahalle/semt (Nisantasi, Besiktas, Levent, Konya merkez)'),
  ('kuafor', 'content_rules', 'anonymous_testimonial', 'Testimonial mutlaka anonim: "musterilerimizden biri", "salon sahibi bir kullanici", "bir kuafor ustasi"'),
  ('kuafor', 'content_rules', 'no_specific_stats', 'YASAK: kaynaksiz spesifik rakam ("%47 daha cok", "geliri 2 katina cikti"). Yumusak ifade: "belirgin fark", "ay sonu kapanisi kisa"'),
  ('kuafor', 'content_rules', 'no_unrealistic_promises', 'YASAK: garantili sonuc, kesin basari, hicbir musteriyi kaybetmezsin')
ON CONFLICT(product, category, key) DO UPDATE SET value=excluded.value, created_at=datetime('now');

-- 4. Kuafor limitations doğrulama/eklenme (var olabilir)
INSERT INTO product_knowledge (product, category, key, value) VALUES
  ('kuafor', 'limitations', 'no_stock_module', 'Stok/envanter/depo modulu YOK. Bu konularda icerik UURETMEYECEK.'),
  ('kuafor', 'limitations', 'no_online_booking', 'Online rezervasyon sayfasi YOK. Sadece SMS hatirlatma + takvim var.'),
  ('kuafor', 'limitations', 'no_accounting_integration', 'Muhasebe yazilim entegrasyonu YOK.'),
  ('kuafor', 'limitations', 'no_pos_integration', 'POS cihazi entegrasyonu YOK. Kasa defteri manuel.'),
  ('kuafor', 'limitations', 'no_online_sales', 'Online satis YOK. Hizmet listesinden secim salon ici.')
ON CONFLICT(product, category, key) DO UPDATE SET value=excluded.value, created_at=datetime('now');

-- 5. Kuafor topic havuzu (planner için onaylı konular)
INSERT INTO product_knowledge (product, category, key, value) VALUES
  ('kuafor', 'topics', 'approved_appointment', 'Randevu yonetimi: otomatik SMS hatirlatma, takvim cakismasi, no-show riski, rebooking'),
  ('kuafor', 'topics', 'approved_staff', 'Personel programlama: vardiya cakismasi, izin takibi, performans raporu'),
  ('kuafor', 'topics', 'approved_cash', 'Kasa/cari: gunluk kapanis, indirim kurallari, bahsis takibi (manuel)'),
  ('kuafor', 'topics', 'approved_customer', 'Musteri portfoyu: sac tipi, alerji, gecmis hizmetler, anonim notlar'),
  ('kuafor', 'topics', 'approved_salon_types', 'Salon tipleri ozellestirme: erkek/kadin/karisik/barber/guzellik'),
  ('kuafor', 'topics', 'approved_multilang', 'Cogul dil: TR/AR (Suriyeli/Arap musteri olan bolgeler)'),
  ('kuafor', 'topics', 'approved_instagram', 'Instagram entegrasyonu: post zamanlama'),
  ('kuafor', 'topics', 'forbidden_stock', 'YASAK konu: stok takibi (modul yok)'),
  ('kuafor', 'topics', 'forbidden_online_rez', 'YASAK konu: online rezervasyon sayfasi'),
  ('kuafor', 'topics', 'forbidden_ecommerce', 'YASAK konu: e-ticaret, online satis')
ON CONFLICT(product, category, key) DO UPDATE SET value=excluded.value, created_at=datetime('now');

-- 6. Doğrulama
SELECT '';
SELECT 'AFTER — kuafor kategori dağilimi:' AS '';
SELECT category, COUNT(*) AS kayit_sayisi
  FROM product_knowledge
 WHERE product='kuafor'
 GROUP BY category
 ORDER BY category;

SELECT '';
SELECT 'Kuafor tone+content_rules detay:' AS '';
SELECT category, key, substr(value, 1, 70) AS value_preview
  FROM product_knowledge
 WHERE product='kuafor' AND category IN ('tone', 'content_rules', 'limitations', 'topics')
 ORDER BY category, key;
