# Proje Kayıt Prompt'u

Yeni bir proje klasöründe Claude Code oturumu aç, aşağıdaki prompt'u yapıştır.
Otomatik olarak projeyi analiz edip merkezi hafıza DB'sine kaydedecek.

---

## PROMPT (Kopyala-Yapıştır)

```
Sen merkezi hafıza sistemime bağlı bir Claude asistansın. Bu projeyi analiz edip tüm bilgileri klipper sunucumdaki hafıza DB'sine kaydet.

## Bağlantı Bilgileri
- **API:** http://YOUR_SERVER_IP:8420/api/v1/memory
- **Auth:** X-Memory-Key: YOUR_MEMORY_API_KEY
- **Cihaz adın:** BURAYA_CIHAZ_ADI (klipper / windows-masaustu / windows-laptop / android-telefon)

## Görevin

### 1. Projeyi Analiz Et
Sırasıyla çalıştır:
- `git log --oneline -50` — commit geçmişi
- `git log --format="%H %s" --since="2 months ago"` — son 2 ay detaylı
- Proje kök dizinindeki package.json, Cargo.toml, pyproject.toml, go.mod, requirements.txt vb. — stack tespiti
- README.md veya docs/ — proje açıklaması
- Klasör yapısını incele (ls, tree --dirsfirst -L 2)
- Test dosyalarını bul ve test sayısını tespit et
- CI/CD config varsa (.github/workflows, wrangler.toml, vercel.json, Dockerfile) incele

### 2. Proje Adını Belirle
Git remote URL veya klasör adından kısa bir proje adı çıkar (küçük harf, tire ile, örn: "petvet", "kuafor", "linux-ai", "renderhane").

### 3. Cihaz-Proje Eşleşmesi Kaydet
```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/device-projects \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d '{"device_name":"CIHAZ","project":"PROJE_ADI","local_path":"TAM_YOL"}'
```

### 4. Mimari Kararları Kaydet (architecture)
Git geçmişi ve kod yapısından çıkar. Her biri için:
- Neden bu stack/framework seçildi?
- Temel tasarım kalıpları (monorepo, microservice, serverless vb.)
- DB seçimi ve nedeni
- Deploy stratejisi
- Önemli entegrasyonlar

```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/discoveries \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d '{"device_name":"CIHAZ","project":"PROJE_ADI","type":"architecture","title":"BAŞLIK","details":"DETAY"}'
```

### 5. Planları Kaydet (plan)
README, issues, TODO, veya commit mesajlarından gelecek planları çıkar:

```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/discoveries \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d '{"device_name":"CIHAZ","project":"PROJE_ADI","type":"plan","title":"BAŞLIK","details":"DETAY"}'
```

### 6. Bug'ları Kaydet
Açık issue'lar, bilinen sorunlar, TODO/FIXME/HACK yorumları:

```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/discoveries \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d '{"device_name":"CIHAZ","project":"PROJE_ADI","type":"bug","title":"BAŞLIK","details":"DETAY"}'
```

### 7. Fix ve Workaround'ları Kaydet
Commit mesajlarında "fix", "hotfix", "workaround", "hack" geçenleri tespit et:

```bash
# type: "fix" veya "workaround"
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/discoveries \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d '{"device_name":"CIHAZ","project":"PROJE_ADI","type":"fix","title":"BAŞLIK","details":"DETAY"}'
```

### 8. Görevleri Kaydet
Son 2 aydaki önemli commit'leri task olarak kaydet (her commit değil, anlamlı iş birimleri):

```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/tasks \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d '{"device_name":"CIHAZ","project":"PROJE_ADI","task":"NE YAPILDI","status":"completed","details":"DETAY"}'
```

### 9. Oturumu Kaydet
En sonda, yaptığın analizi oturum olarak kaydet:

```bash
curl -s -X POST http://YOUR_SERVER_IP:8420/api/v1/memory/sessions \
  -H "Content-Type: application/json" \
  -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  -d '{"device_name":"CIHAZ","summary":"PROJE_ADI proje analizi ve hafıza DB kaydı. X architecture, Y plan, Z bug, W fix, T task kaydedildi.","tasks_completed":["proje analizi","hafıza DB kaydı"],"notes":"İlk kayıt"}'
```

### 10. Sonuç Raporu
Bitince şu formatla özet ver:

```
✅ PROJE_ADI — Hafıza Kaydı Tamamlandı
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Stack:     ...
Deploy:    ...
Test:      ... adet
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mimari:    X kayıt
Plan:      X kayıt  
Bug:       X kayıt
Fix:       X kayıt
Workaround:X kayıt
Task:      X kayıt
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Oturum:    #N kaydedildi
```

## Kurallar
- Türkçe çalış
- Onay bekleme, direkt analiz et ve kaydet
- Her API çağrısının başarılı olduğunu kontrol et
- Gereksiz detay verme, özlü ve net kayıtlar yap
- Title'lar kısa (max 60 karakter), details açıklayıcı olsun
- Aynı bilgiyi tekrar kaydetme (önce mevcut kayıtları kontrol et)

## Mevcut Kayıtları Kontrol
Başlamadan önce bu projenin zaten kaydı var mı kontrol et:
```bash
curl -s -H "X-Memory-Key: YOUR_MEMORY_API_KEY" \
  "http://YOUR_SERVER_IP:8420/api/v1/memory/projects/PROJE_ADI"
```

Varsa sadece eksikleri tamamla, yoksa sıfırdan kaydet.

Şimdi başla — projeyi analiz et ve her şeyi kaydet.
```
