# n8n Workflow Review: Google Maps Email Scraper

**Workflow:** Scrape business emails from Google Maps without the use of any third party APIs  
**Template:** #2567  
**Sonuc:** Calismaz  
**Tarih:** 2026-04-11  

---

## Neden Calismaz

Google Maps bir JavaScript SPA (Single Page Application). Tarayicida gordugun isletme listesi sunucudan HTML olarak gelmez — JavaScript tarafindan render edilir.

```
Tarayicida olan:
  1. HTML iskeleti gelir (bos sayfa)
  2. ~2MB JavaScript yuklenir
  3. JS, Google'in dahili API'sine istek atar
  4. Sonuclar JSON olarak doner
  5. JS, sonuclari sayfaya render eder

n8n HTTP Request'in yaptigi:
  1. HTML iskeleti gelir (bos sayfa)
  2. Bitti.
```

Workflow'un "Search Google Maps with query" node'u sadece 1. adimi yapar. Isletme URL'leri, isimleri, telefon numaralari — hicbiri gelmez. Geri kalan tum pipeline (URL scrape, email scrape, filter, Google Sheets) bos veri uzerinde calisir.

---

## Calisan Alternatifler

### 1. Google Places API (Onerilen)
- Resmi, guvenilir, yasal
- Ayda $200 ucretsiz kredi (cogu kullanim icin yeter)
- Isletme adi, adres, telefon, web sitesi URL'si doner
- Email dogrudan vermez ama web sitesi URL'sinden scrape edilebilir
- n8n HTTP Request node ile dogrudan kullanilir

### 2. SerpAPI / Outscraper
- Google Maps sonuclarini API olarak sunar
- Ucretli ama sonuc garantili
- n8n entegrasyonu kolay

### 3. Headless Browser (Puppeteer/Playwright)
- Google'i gercek tarayici gibi gorur
- n8n'de direkt node yok, custom API gerektirirr
- En yakin "bedava" cozum ama bakim maliyeti yuksek
