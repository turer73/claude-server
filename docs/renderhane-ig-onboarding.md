# Renderhane Instagram — Onboarding (kullanıcı tarafı)

`@render.hane` hesabını panola-social engine'e bağlamak için Meta tarafında 5 adım. Kodu yazacak agent token'ı `.env`'de bekliyor; aşağıdakiler tamamlanmadan multi-account refactor anlamlı değil.

## 1. Instagram hesabını Business'a çevir

`@render.hane` → Settings → Account → Switch to Professional → **Business** seç (Creator değil; insights API farklı izin gerektirir). Kategori: Software / Photography / Brand. İletişim e-postası: `info@renderhane.com`.

## 2. Facebook Page bağla (zorunlu)

Instagram Graph API token Facebook Page üzerinden çalışır.
- Facebook'ta yeni Page: "Renderhane" (Software/Brand kategori).
- IG Settings → Connected Accounts → Facebook → Page'i bağla.

## 3. Meta Developer App seç

İki seçenek:

**A) Mevcut Panola App'i kullan** (önerilen — token yönetimi tek yerden):
- App ID: `1724334692308147` (panola social engine zaten kullanıyor)
- developers.facebook.com → My Apps → Panola → Roles → kendini Admin/Developer olarak ekle (kullanıcı olarak).
- App Settings → Add Platform → Instagram (zaten ekli olabilir).

**B) Ayrı Renderhane App** (brand isolation isterken):
- developers.facebook.com → Create App → "Business" type → "Renderhane Social".
- Add Product → Instagram → Basic Display + Graph API.

A önerilir; tek developer (turer73), iki ayrı App yönetmek değer üretmez.

## 4. App'e izin ver (4 scope)

Instagram Graph API → Permissions:
- `instagram_basic` — hesap meta verisi
- `instagram_content_publish` — post yayınlama
- `pages_show_list` — Page'leri listeleme (token üretimi için gerekli)
- `instagram_manage_insights` — metric çekme (collect-metrics bunu kullanıyor)

İlk 3'ü standard, **4. izin App Review gerektirebilir** (Business verification varsa otomatik). Renderhane'de mevcut Page yetkili olduğu için review aşamasına gerek kalmadan kullanılabilir.

## 5. Long-lived token üret

Graph API Explorer (developers.facebook.com/tools/explorer):
1. App: Panola seç (veya Renderhane App)
2. User Token → Get User Access Token → 4 scope'u işaretle → onay
3. Üretilen short-lived (1 saat) token'ı al
4. Long-lived'a çevir:
   ```
   curl -G "https://graph.facebook.com/v25.0/oauth/access_token" \
     -d "grant_type=fb_exchange_token" \
     -d "client_id=1724334692308147" \
     -d "client_secret=<META_APP_SECRET>" \
     -d "fb_exchange_token=<short_lived>"
   ```
5. Page Access Token'a çevir:
   ```
   curl "https://graph.facebook.com/v25.0/me/accounts?access_token=<long_lived_user>"
   ```
   Renderhane Page'in `access_token` field'ını al — **bu 60 gün geçerli, page-level**.
6. IG Business Account ID'yi öğren:
   ```
   curl "https://graph.facebook.com/v25.0/<page_id>?fields=instagram_business_account&access_token=<page_token>"
   ```

## 6. Sonuçları teslim et

Aşağıdaki 3 değeri agent'a (yeni oturum) ver:

```
RENDERHANE_INSTAGRAM_TOKEN=<page_access_token>
RENDERHANE_INSTAGRAM_USER_ID=<ig_business_account_id>
RENDERHANE_PAGE_ID=<facebook_page_id>
```

VPS `.env`'e eklenecek (`/opt/panola-social/.env`). Multi-account refactor sonra başlayabilir.

## Tahmini süre

- Adım 1-2: 5 dakika (UI tıklama)
- Adım 3-4: 10-15 dakika (App Review yoksa)
- Adım 5: 10 dakika (curl ile)

Toplam ~30 dakika. App Review gerekirse 1-3 iş günü ekle (instagram_manage_insights için).

## Risk notları

- **Token süresi:** 60 gün. `token-refresh.sh` halihazırda panola.app için çalışıyor, render.hane için aynı script'i hesap-aware yapmak Phase 2 işi.
- **Yeni hesap algoritma penaltısı:** İlk 4-6 hafta organic reach düşük (~1-3% ER). Bu sürede manuel/yarı-otomatik post + community engagement ağırlıkta olmalı, full automation 5. haftadan sonra.
- **App Review riski:** instagram_manage_insights review'a takılırsa metric collection panola.app token'ıyla render.hane post ID'lerini sorgulayamaz (cross-account izin yok). En kötü senaryoda iki ayrı App.
