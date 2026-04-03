# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: petvet/02-auth-flow.spec.ts >> PetVet — Auth & Dashboard >> full login flow (phone + profile + PIN)
- Location: tests/petvet/02-auth-flow.spec.ts:36:7

# Error details

```
Error: Still on auth screen

expect(received).toBe(expected) // Object.is equality

Expected: false
Received: true
```

# Page snapshot

```yaml
- generic [ref=e4]:
  - generic [ref=e5]:
    - generic [ref=e6]: 🐾
    - heading "Patili Dostlar Veteriner" [level=1] [ref=e7]
    - paragraph [ref=e8]: Dr. Selin — PIN girin
  - generic [ref=e9]:
    - generic [ref=e10]: 👑
    - generic [ref=e11]: Dr. Selin
    - textbox [active]
    - generic [ref=e17]:
      - button "1" [ref=e18]
      - button "2" [ref=e19]
      - button "3" [ref=e20]
      - button "4" [ref=e21]
      - button "5" [ref=e22]
      - button "6" [ref=e23]
      - button "7" [ref=e24]
      - button "8" [ref=e25]
      - button "9" [ref=e26]
      - button "0" [ref=e28]
      - button "←" [ref=e29]
    - paragraph [ref=e30]: PIN hatali
    - button "← Geri" [ref=e31]
  - button "Isletmeden cikis yap" [ref=e33]
```

# Test source

```ts
  1  | import { test, expect } from "@playwright/test";
  2  | 
  3  | const PHONE = process.env.PETVET_PHONE || "5559998877";
  4  | const PASS = process.env.PETVET_PASS || "test1234";
  5  | const PIN = process.env.PETVET_PIN || "1234";
  6  | 
  7  | async function login(page: import("@playwright/test").Page) {
  8  |   await page.goto("/login");
  9  | 
  10 |   await page.locator('input[type="tel"], input[type="text"]').first().fill(PHONE);
  11 |   await page.locator('input[type="password"]').first().fill(PASS);
  12 | 
  13 |   await page.locator(
  14 |     'button[type="submit"], button:has-text("Giriş"), button:has-text("Devam")'
  15 |   ).first().click();
  16 | 
  17 |   // Profile selection — click "Sahip" profile
  18 |   const sahipCard = page.locator('text=Sahip').first();
  19 |   await sahipCard.waitFor({ timeout: 5000 });
  20 |   await sahipCard.click();
  21 | 
  22 |   // PIN entry — click numpad digits
  23 |   const pinPage = page.locator('text=PIN girin');
  24 |   if (await pinPage.isVisible({ timeout: 3000 }).catch(() => false)) {
  25 |     for (const digit of PIN) {
  26 |       await page.locator(`button:has-text("${digit}")`).first().click();
  27 |       await page.waitForTimeout(200);
  28 |     }
  29 |   }
  30 | 
  31 |   // Wait for dashboard to load
  32 |   await page.waitForTimeout(2000);
  33 | }
  34 | 
  35 | test.describe("PetVet — Auth & Dashboard", () => {
  36 |   test("full login flow (phone + profile + PIN)", async ({ page }) => {
  37 |     await login(page);
  38 |     // Should not see PIN or login form
  39 |     const content = await page.content();
  40 |     const stillOnAuth = content.includes("PIN girin") || content.includes("Kim kullanıyor");
> 41 |     expect(stillOnAuth, "Still on auth screen").toBe(false);
     |                                                 ^ Error: Still on auth screen
  42 |   });
  43 | 
  44 |   test("dashboard has navigation", async ({ page }) => {
  45 |     await login(page);
  46 |     // Look for any links or navigation elements
  47 |     const links = page.locator('a[href]');
  48 |     const count = await links.count();
  49 |     expect(count).toBeGreaterThan(0);
  50 |   });
  51 | 
  52 |   test("no application errors", async ({ page }) => {
  53 |     await login(page);
  54 |     const content = await page.content();
  55 |     expect(content).not.toContain("Application error");
  56 |     expect(content).not.toContain("500 Internal");
  57 |   });
  58 | });
  59 | 
```