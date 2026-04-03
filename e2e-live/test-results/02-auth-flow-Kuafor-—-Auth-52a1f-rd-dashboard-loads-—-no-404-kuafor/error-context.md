# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: kuafor/02-auth-flow.spec.ts >> Kuafor — Auth & Dashboard >> dashboard loads — no 404
- Location: tests/kuafor/02-auth-flow.spec.ts:27:7

# Error details

```
Error: Dashboard shows 404 — routing bug in Kuafor!

expect(received).toBe(expected) // Object.is equality

Expected: false
Received: true
```

# Page snapshot

```yaml
- generic [ref=e3]:
  - banner [ref=e4]:
    - heading "Panola" [level=1] [ref=e5]
    - generic [ref=e6]:
      - generic [ref=e7]: Ahmet Yılmaz
      - button "Cikis" [ref=e8]
  - main [ref=e9]:
    - generic [ref=e10]: 404 -- Sayfa bulunamadi
  - navigation [ref=e11]:
    - link "📅 Randevular" [ref=e12] [cursor=pointer]:
      - /url: /
      - generic [ref=e13]: 📅
      - text: Randevular
    - link "👤 Personel" [ref=e14] [cursor=pointer]:
      - /url: /staff
      - generic [ref=e15]: 👤
      - text: Personel
    - link "✂️ Hizmetler" [ref=e16] [cursor=pointer]:
      - /url: /services
      - generic [ref=e17]: ✂️
      - text: Hizmetler
    - link "💰 Kasa" [ref=e18] [cursor=pointer]:
      - /url: /cash
      - generic [ref=e19]: 💰
      - text: Kasa
    - link "📊 Raporlar" [ref=e20] [cursor=pointer]:
      - /url: /reports
      - generic [ref=e21]: 📊
      - text: Raporlar
    - link "📸 Instagram" [ref=e22] [cursor=pointer]:
      - /url: /instagram
      - generic [ref=e23]: 📸
      - text: Instagram
    - link "📘 Facebook" [ref=e24] [cursor=pointer]:
      - /url: /facebook
      - generic [ref=e25]: 📘
      - text: Facebook
    - link "✨ AI Stilist" [ref=e26] [cursor=pointer]:
      - /url: /ai-stylist
      - generic [ref=e27]: ✨
      - text: AI Stilist
    - link "⚙️ Ayarlar" [ref=e28] [cursor=pointer]:
      - /url: /settings
      - generic [ref=e29]: ⚙️
      - text: Ayarlar
```

# Test source

```ts
  1  | import { test, expect } from "@playwright/test";
  2  | 
  3  | const PHONE = process.env.KUAFOR_PHONE || "5551110001";
  4  | const PASS = process.env.KUAFOR_PASS || "test1234";
  5  | 
  6  | async function login(page: import("@playwright/test").Page) {
  7  |   await page.goto("/login");
  8  | 
  9  |   await page.locator('input[type="tel"], input[type="text"]').first().fill(PHONE);
  10 |   await page.locator('input[type="password"]').first().fill(PASS);
  11 | 
  12 |   await page.locator(
  13 |     'button:has-text("Giriş Yap"), button:has-text("Giriş"), button[type="submit"]'
  14 |   ).first().click();
  15 | 
  16 |   // SPA — wait for content to change
  17 |   await page.waitForTimeout(3000);
  18 | }
  19 | 
  20 | test.describe("Kuafor — Auth & Dashboard", () => {
  21 |   test("login succeeds", async ({ page }) => {
  22 |     await login(page);
  23 |     const passVisible = await page.locator('input[type="password"]').isVisible().catch(() => false);
  24 |     expect(passVisible).toBe(false);
  25 |   });
  26 | 
  27 |   test("dashboard loads — no 404", async ({ page }) => {
  28 |     await login(page);
  29 |     const content = await page.content();
  30 |     // This is a REAL BUG if it fails — dashboard shows 404
  31 |     const has404 = content.includes("Sayfa bulunamad");
> 32 |     expect(has404, "Dashboard shows 404 — routing bug in Kuafor!").toBe(false);
     |                                                                    ^ Error: Dashboard shows 404 — routing bug in Kuafor!
  33 |   });
  34 | 
  35 |   test("sidebar navigation visible on desktop", async ({ page }) => {
  36 |     await login(page);
  37 |     // Desktop viewport — sidebar should be visible (not the mobile bottom nav)
  38 |     // Look for sidebar links by text content
  39 |     const sidebarTexts = ["Randevu", "Personel", "Hizmet", "Kasa", "Ayar"];
  40 |     let found = 0;
  41 |     for (const text of sidebarTexts) {
  42 |       const el = page.locator(`a:has-text("${text}"), button:has-text("${text}")`).first();
  43 |       if (await el.isVisible().catch(() => false)) found++;
  44 |     }
  45 |     expect(found, "Expected sidebar nav items").toBeGreaterThan(0);
  46 |   });
  47 | 
  48 |   test("randevular page loads", async ({ page }) => {
  49 |     await login(page);
  50 |     const randevuLink = page.locator('a:has-text("Randevu")').first();
  51 |     if (await randevuLink.isVisible().catch(() => false)) {
  52 |       await randevuLink.click();
  53 |       await page.waitForTimeout(2000);
  54 |       const content = await page.content();
  55 |       expect(content).not.toContain("Application error");
  56 |     }
  57 |   });
  58 | });
  59 | 
```