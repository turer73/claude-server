# n8n Workflow Review: Google Maps Email Scraper

**Workflow:** Scrape business emails from Google Maps without the use of any third party APIs  
**ID:** 4xv9sorBHgZy2D7t  
**Template:** #2567  
**Durum:** Inactive  
**Tarih:** 2026-04-11  

---

## 1. Genel Bakis

Bu workflow, Google Maps uzerinde arama yaparak isletmelerin web sitelerini buluyor, bu siteleri ziyaret edip e-posta adreslerini scrape ediyor ve sonuclari Google Sheets'e kaydediyor.

### Akis Diyagrami

```
ORKESTRATOR (Ust Akis):
  Run workflow (Manuel Trigger)
    -> Loop over queries
      -> Execute scraper for query (sub-workflow olarak kendini cagirir)
      -> Wait 2s
      -> Loop over queries (dongu)

SCRAPER (Alt Akis — Sub-workflow):
  Starts scraper workflow (Execute Workflow Trigger)
    -> Search Google Maps with query (HTTP GET)
    -> Scrape URLs from results (Code)
    -> Filter irrelevant URLs (Filter)
    -> Remove Duplicate URLs
    -> Loop over URLs
      -> Request web page for URL (HTTP GET)
      -> Loop over URLs (dongu)
    -> Loop over pages
      -> Scrape emails from page (Code)
      -> Loop over pages (dongu)
    -> Aggregate arrays of emails
    -> Split out into default data structure
    -> Remove duplicate emails
    -> Filter irrelevant emails
    -> Save emails to Google Sheet
```

---

## 2. Mimari Analiz

Workflow iki katmanli bir mimari kullaniyor:

**Katman 1 — Orkestrator:** Manuel tetikleme ile baslar. Birden fazla arama sorgusunu sirayla isler. Her sorgu icin ayni workflow'u sub-workflow olarak calistirir (`$workflow.id` kullanarak). Sorgular arasi 2 saniye bekleme suresi var.

**Katman 2 — Scraper:** `executeWorkflowTrigger` ile tetiklenir. Google Maps'te arama yapar, sonuclardan URL'leri cikarir, her URL'nin web sayfasini indirir, e-postalari regex ile bulur ve Google Sheets'e kaydeder.

**Avantajlar:**
- Self-referencing pattern ile tek workflow'da iki islem
- Sub-workflow'lar paralel calisabilir (`waitForSubWorkflow: false`)
- Basit ve anlasilir yapi

**Dezavantajlar:**
- Google Maps SPA yapisi nedeniyle HTTP GET guvenilir degil
- Hata yonetimi zayif (null check eksikligi)
- Rate limiting sadece sorgular arasi, URL istekleri arasinda yok

---

## 3. Node-by-Node Inceleme

### 3.1 Run workflow (Manuel Trigger)
- **Tip:** `n8n-nodes-base.manualTrigger`
- **Gorev:** Workflow'u manuel baslatir
- **Durum:** OK — Sorgu listesi kullanicinin burada tanimlamasi gerekiyor

### 3.2 Loop over queries (SplitInBatches)
- **Tip:** `n8n-nodes-base.splitInBatches` v3
- **Gorev:** Sorgu listesini sirayla isler
- **Durum:** OK

### 3.3 Execute scraper for query
- **Tip:** `n8n-nodes-base.executeWorkflow` v1.1
- **Yapilandirma:** `workflowId: $workflow.id`, `mode: each`, `waitForSubWorkflow: false`
- **Durum:** OK — Kendini sub-workflow olarak cagirir, beklemeden devam eder

### 3.4 Wait between executions
- **Tip:** `n8n-nodes-base.wait` v1.1
- **Yapilandirma:** 2 saniye bekleme
- **Durum:** OK — Rate limiting icin uygun

### 3.5 Starts scraper workflow (Execute Workflow Trigger)
- **Tip:** `n8n-nodes-base.executeWorkflowTrigger` v1
- **Gorev:** Sub-workflow giris noktasi
- **Durum:** OK

### 3.6 Search Google Maps with query
- **Tip:** `n8n-nodes-base.httpRequest` v4.2
- **URL:** `https://www.google.com/maps/search/{{ $json.query }}`
- **Sorun:** ORTA — Google Maps JavaScript-agirlikli bir SPA. Basit HTTP GET istegi ile arama sonuclari tam olarak donmeyebilir. Isletme URL'leri JavaScript data blob'larinda gomulu olabilir. Sonuclar degisken ve guvenilmez olacaktir.

### 3.7 Scrape URLs from results
- **Tip:** `n8n-nodes-base.code` v2
- **Kod:**
```javascript
const data = $input.first().json.data
const regex = /https?:\/\/[^\/]+/g
const urls = data.match(regex)
return urls.map(url => ({json: {url: url}}))
```
- **Sorun 1:** KRITIK — `data.match(regex)` eslesme bulamazsa `null` doner. `null.map()` cagrisi **TypeError** firlatirak workflow'u durdurur.
- **Sorun 2:** ORTA — Regex cok basit. Sadece scheme + domain yakalaniyor (`https://example.com`), path dahil edilmiyor. Google Maps HTML'indeki CDN, API, tracking gibi tum URL'ler de yakalaniyor.

**Duzeltilmis kod:**
```javascript
const data = $input.first().json.data
const regex = /https?:\/\/(?!(?:www\.)?(?:google|gstatic|googleapis|ggpht)\.)([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/g
const urls = data.match(regex) || []
const uniqueUrls = [...new Set(urls)]
return uniqueUrls.map(url => ({json: {url: url}}))
```

### 3.8 Filter irrelevant URLs
- **Tip:** `n8n-nodes-base.filter` v2.2
- **Kosul 1:** URL `notRegex` — `(google|gstatic|ggpht|schema\.org|example\.com|sentry-next\.wixpress\.com|imli\.com|sentry\.wixpress\.com|ingest\.sentry\.io)` 
- **Kosul 2:** `""` equals `""` (bos kosul)
- **Sorun:** ORTA — Ikinci kosul tamamen bos. `leftValue` ve `rightValue` ikisi de bos string. `"" == ""` her zaman `true` dondugu icin sonucu etkilemiyor ama acikca bir yapilandirma hatasi veya kalinti. Kaldirilmali.

### 3.9 Remove Duplicate URLs
- **Tip:** `n8n-nodes-base.removeDuplicates` v1.1
- **Durum:** OK

### 3.10 Loop over URLs (SplitInBatches)
- **Tip:** `n8n-nodes-base.splitInBatches` v3
- **Yapilandirma:** `reset: false`, `onError: continueErrorOutput`
- **Durum:** OK — Hata durumunda devam ediyor

### 3.11 Request web page for URL
- **Tip:** `n8n-nodes-base.httpRequest` v4.2
- **URL:** `{{ $json.url }}`
- **Yapilandirma:** `onError: continueRegularOutput`
- **Sorun 1:** DUSUK — Timeout ayari yok. Yavas siteler workflow'u asabilir.
- **Sorun 2:** DUSUK — Content-Type kontrolu yok. PDF, resim gibi binary icerikleri de indirmeye calisir.
- **Sorun 3:** DUSUK — Boyut limiti yok. Cok buyuk sayfalar bellek sorunlarina yol acabilir.

### 3.12 Loop over pages (SplitInBatches)
- **Tip:** `n8n-nodes-base.splitInBatches` v3
- **Yapilandirma:** `onError: continueErrorOutput`
- **Durum:** OK — "Loop over URLs" tamamlaninca toplanan HTTP response'lari burada tek tek islenir

### 3.13 Scrape emails from page
- **Tip:** `n8n-nodes-base.code` v2
- **Mod:** `runOnceForEachItem`
- **Kod:**
```javascript
const data = $json.data
const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?!png|jpg|gif|jpeg)[a-zA-Z]{2,}/g
const emails = data.match(emailRegex)
return {json: {emails: emails}}
```
- **Sorun 1:** KRITIK — `data.match(emailRegex)` eslesme bulamazsa `null` doner. Downstream node'lar (Aggregate, Split Out) `null` degerle dogru calismayabilir.
- **Sorun 2:** DUSUK — Negative lookahead'de eksik dosya uzantilari: `.webp`, `.svg`, `.css`, `.js`, `.woff`, `.woff2`, `.ico`, `.map` gibi uzantilar da filtrelenmeli.
- **Sorun 3:** DUSUK — `$json.data` undefined olabilir (HTTP istegi basarisiz olduysa). `undefined.match()` da TypeError firlatirir.

**Duzeltilmis kod:**
```javascript
const data = $json.data || ''
const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?!png|jpg|gif|jpeg|webp|svg|css|js|woff2?|ico|map\b)[a-zA-Z]{2,}/g
const emails = data.match(emailRegex) || []
return {json: {emails: emails}}
```

### 3.14 Aggregate arrays of emails
- **Tip:** `n8n-nodes-base.aggregate` v1
- **Yapilandirma:** `fieldToAggregate: emails`, `mergeLists: true`
- **Durum:** OK — Tum email dizilerini tek dizide birlestiriyor

### 3.15 Split out into default data structure
- **Tip:** `n8n-nodes-base.splitOut` v1
- **Yapilandirma:** `fieldToSplitOut: emails`
- **Durum:** OK

### 3.16 Remove duplicate emails
- **Tip:** `n8n-nodes-base.removeDuplicates` v1.1
- **Yapilandirma:** `compare: selectedFields`, `fieldsToCompare: emails`
- **Durum:** OK

### 3.17 Filter irrelevant emails
- **Tip:** `n8n-nodes-base.filter` v2.2
- **Kosul:** Email `notRegex` — `(google|gstatic|ggpht|schema\.org|example\.com|sentry\.wixpress\.com|sentry-next\.wixpress\.com|ingest\.sentry\.io|sentry\.io|imli\.com)`
- **Durum:** OK — URL filtresinden daha temiz, tek kosul

### 3.18 Save emails to Google Sheet
- **Tip:** `n8n-nodes-base.googleSheets` v4.5
- **Credentials:** Google Sheets OAuth2 (ID: HncC0A3Ykt90dJNd)
- **Sorun:** BILGI — `documentId` ve `sheetName` bos. Calistirmadan once mutlaka ayarlanmali.

---

## 4. Kritik Buglar

### Bug #1: Null Reference — Scrape URLs from results
**Node:** Scrape URLs from results  
**Satir:** `return urls.map(url => ({json: {url: url}}))`  
**Sorun:** `data.match(regex)` hic eslesme bulamazsa `null` doner. `null.map()` **TypeError** firlatirak workflow'u durdurur.  
**Etki:** Google Maps sonuc donmezse veya yanit formatinda degisiklik olursa workflow tamamen durur.  
**Cozum:** `const urls = data.match(regex) || []`

### Bug #2: Null Reference — Scrape emails from page  
**Node:** Scrape emails from page  
**Satir:** `return {json: {emails: emails}}`  
**Sorun:** `emails` degeri `null` olabilir. Downstream Aggregate node'u null'lari birlestiremeyebilir.  
**Etki:** E-posta icermeyen sayfalar workflow'u bozabilir.  
**Cozum:** `const emails = data.match(emailRegex) || []`

### Bug #3: Bos Filter Kosulu
**Node:** Filter irrelevant URLs  
**Sorun:** Ikinci kosulda `leftValue` ve `rightValue` ikisi de bos string. Bu kosul her zaman true doner.  
**Etki:** Islevsel zarar yok ama yapilandirma hatasi. Ileride yanlis sonuclara yol acabilir.  
**Cozum:** Ikinci kosulu kaldirin.

---

## 5. Performans ve Guvenilirlik Sorunlari

### 5.1 Google Maps SPA Sorunu
Google Maps JavaScript-agirlikli bir Single Page Application. Basit HTTP GET istegi ile tam arama sonuclari donmeyebilir. Isletme URL'leri sayfa iceriginde JavaScript data blob'larinda gomulu olabilir.

**Oneriler:**
- n8n'de headless browser destegi varsa kullanin
- Google Places API (resmi) daha guvenilir sonuc verir (ancak workflow'un amaci 3. parti API kullanmamak)
- Response icerigini loglayin ve gercekte ne geldigini kontrol edin

### 5.2 Rate Limiting Eksikligi
Sorgular arasi 2 saniye bekleme var ama bireysel web sitesi istekleri arasinda hic bekleme yok. 50+ URL varsa saniyeler icinde onlarca istek atilir.

**Riskler:**
- Hedef siteler IP'nizi engelleyebilir
- Google rate limit uygulayabilir
- n8n instance'i bellek sorunlari yasayabilir

**Oneri:** Loop over URLs node'unda batch size'i 1 yapin ve araya Wait node'u ekleyin.

### 5.3 Timeout ve Boyut Limiti Yok
HTTP isteklerinde timeout belirlenmemis. Yavas veya buyuk siteler workflow'u asabilir.

**Oneri:** HTTP Request node'larinda `timeout: 10000` (10 saniye) ve response boyutu icin kontrol ekleyin.

### 5.4 Sayfalama (Pagination) Yok
Google Maps sadece ilk sayfa sonuclarini donduruyor. Genis aramalar icin sonuc sayisi sinirli kalacaktir.

---

## 6. Guvenlik ve Yasal Uyarilar

### 6.1 Google Hizmet Sartlari
Google Maps'i otomatik scrape etmek Google'in Hizmet Sartlari'na (ToS) aykiridir. Google bu tur istekleri tespit edip IP engellemesi uygulayabilir veya CAPTCHA gosterebilir.

### 6.2 GDPR / KVKK Uyumlulugu
Isletmelerin web sitelerinden izinsiz e-posta toplamak:
- AB'de GDPR'a aykiri olabilir
- Turkiye'de KVKK kapsaminda kisisel veri isleme sayilabilir
- Anti-spam mevzuatina (CAN-SPAM, PECR) aykiri olabilir

Toplanan e-postalarin reklam/pazarlama amacli kullanilmasi ek yasal sorumluluklar dogurur.

### 6.3 Credential Guvenligi
Google Sheets OAuth2 credential'i workflow JSON'unda ID olarak gorunuyor (`HncC0A3Ykt90dJNd`). Workflow'u paylasiyor veya versiyonluyorsaniz credential bilgilerinin guvenligine dikkat edin.

---

## 7. Iyilestirme Onerileri

### 7.1 Null-safe Code Node'lari
Tum Code node'larinda `match()` sonucuna `|| []` ekleyin:

```javascript
// Scrape URLs from results — Duzeltilmis
const data = $input.first().json.data || ''
const regex = /https?:\/\/[^\/\s"'<>]+/g
const urls = data.match(regex) || []
return urls.map(url => ({json: {url: url}}))
```

```javascript
// Scrape emails from page — Duzeltilmis
const data = $json.data || ''
const emailRegex = /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?!png|jpg|gif|jpeg|webp|svg|css|js|woff2?|ico|map\b)[a-zA-Z]{2,}/g
const emails = data.match(emailRegex) || []
return {json: {emails: emails}}
```

### 7.2 URL Regex Iyilestirmesi
Daha hedefli URL yakalama:
```javascript
const regex = /https?:\/\/(?!(?:www\.)?(?:google|gstatic|googleapis|ggpht|schema\.org)\b)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:\/[^\s"'<>]*)?/g
```

### 7.3 Rate Limiting Ekleme
Loop over URLs icerisine Wait node'u ekleyin (0.5-1 saniye arasi).

### 7.4 HTTP Request Timeout
Her iki HTTP Request node'una da timeout ekleyin:
- Search Google Maps: `timeout: 15000`
- Request web page: `timeout: 10000`

### 7.5 Bos Filter Kosulunu Kaldirma
"Filter irrelevant URLs" node'undaki ikinci bos kosulu silin.

---

## 8. Ozet Tablo

| # | Seviye | Sorun | Node | Oneri |
|---|--------|-------|------|-------|
| 1 | KRITIK | `data.match(regex)` null donebilir, `.map()` crash | Scrape URLs from results | `\|\| []` ekle |
| 2 | KRITIK | `data.match(emailRegex)` null donebilir | Scrape emails from page | `\|\| []` ekle |
| 3 | ORTA | Ikinci filter kosulu bos | Filter irrelevant URLs | Kosulu kaldir |
| 4 | ORTA | URL regex cok genis, sadece domain yakalar | Scrape URLs from results | Regex iyilestir |
| 5 | ORTA | Google Maps SPA, HTTP GET guvenilmez | Search Google Maps | Response kontrolu ekle |
| 6 | DUSUK | Email regex eksik uzanti filtreleri | Scrape emails from page | Regex genislet |
| 7 | DUSUK | URL istekleri arasi rate limit yok | Loop over URLs | Wait node ekle |
| 8 | DUSUK | HTTP timeout/boyut limiti yok | Request web page for URL | Timeout ekle |
| 9 | BILGI | Google Sheets yapilandirilmamis | Save emails to Google Sheet | documentId/sheetName ayarla |
| 10 | BILGI | Sayfalama yok | Search Google Maps | Pagination mantigi ekle |

---

## 9. Uygulanan Duzeltmeler

Duzeltilmis workflow: `n8n-workflows/google-maps-email-scraper-fixed.json`

### Duzeltme Tablosu

| # | Orijinal Sorun | Duzeltme | Dosyadaki Yer |
|---|----------------|----------|---------------|
| 1 | `data.match(regex)` null → crash | `\|\| []` + bos sonuc icin bilgi mesaji | Scrape URLs from results — jsCode |
| 2 | `data.match(emailRegex)` null | `$json.data \|\| ''` + `\|\| []` + bos data kontrolu | Scrape emails from page — jsCode |
| 3 | Bos filter kosulu ("" == "") | Ikinci kosul tamamen kaldirildi | Filter irrelevant URLs — conditions |
| 4 | URL regex cok genis | Pre-filtering + `new URL()` ile domain cikartma + exclude pattern | Scrape URLs from results — jsCode |
| 5 | Google Maps bot tespiti | User-Agent + Accept-Language header eklendi | Search Google Maps — options.headers |
| 6 | Email regex eksik filtreler | .webp, .svg, .css, .js, .woff2, .ico, .map, .ttf, .eot, .pdf eklendi | Scrape emails from page — jsCode |
| 7 | URL istekleri arasi rate limit yok | "Wait between URL requests" node'u eklendi (1s) | Yeni node + connection degisikligi |
| 8 | HTTP timeout yok | Google Maps: 15s, web page: 10s timeout | Her iki HTTP Request node — options.timeout |
| 9 | Gecersiz email'ler gecebiliyor | Post-filter: kisa email, IP domain, placeholder temizleme | Scrape emails from page — jsCode |
| 10 | Email filter'da noreply eksik | `noreply\|no-reply\|mailer-daemon\|postmaster` eklendi | Filter irrelevant emails — rightValue |

### Duzeltilemeyenler (Durust Degerlendirme)

| Sorun | Neden Duzeltilemez |
|-------|-------------------|
| Google Sheets document/sheet bos | Kullanicinin kendi Google hesabindan secmesi gerekiyor. n8n UI'da yapilmali. |
| Google Maps SPA sorunu | Temel mimari kisitlama. HTTP GET ile JavaScript-rendered icerigi almak mumkun degil. Headless browser veya resmi API gerekir. Workflow'un "no third party API" vaadi bu noktada kisitidir. |
| Sayfalama (pagination) yok | Google Maps sonuclari JavaScript ile yukleniyor. Sayfalama icin tamamen farkli bir yaklasim gerekir (headless browser, Maps API). Mevcut HTTP GET mimarisine eklenemez. |
| Google ToS ihlali riski | Teknik duzeltme degil, yasal/etik karar. Kullanici farkinda olmali. |

### Yeni Eklenen Node

**Wait between URL requests** — URL istekleri arasina 1 saniye bekleme ekler.
- ID: `b1c2d3e4-f5a6-7890-abcd-ef1234567890`
- Baglanti degisikligi: `Request web page for URL` → ~~`Loop over URLs`~~ → `Wait between URL requests` → `Loop over URLs`

### Akis Degisikligi (Onceki vs Sonraki)

```
ONCEKI:
  Request web page for URL → Loop over URLs (dogrudan)

SONRAKI:
  Request web page for URL → Wait 1s → Loop over URLs (rate limited)
```
