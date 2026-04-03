import { test, expect } from "@playwright/test";

const PHONE = process.env.KUAFOR_PHONE || "REDACTED_PHONE";
const PASS = process.env.KUAFOR_PASS || "test1234";

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");

  await page.locator('input[type="tel"], input[type="text"]').first().fill(PHONE);
  await page.locator('input[type="password"]').first().fill(PASS);

  await page.locator(
    'button:has-text("Giriş Yap"), button:has-text("Giriş"), button[type="submit"]'
  ).first().click();

  // SPA — wait for content to change
  await page.waitForTimeout(3000);
}

test.describe("Kuafor — Auth & Dashboard", () => {
  test("login succeeds", async ({ page }) => {
    await login(page);
    const passVisible = await page.locator('input[type="password"]').isVisible().catch(() => false);
    expect(passVisible).toBe(false);
  });

  test("dashboard loads — no 404", async ({ page }) => {
    await login(page);
    const content = await page.content();
    // This is a REAL BUG if it fails — dashboard shows 404
    const has404 = content.includes("Sayfa bulunamad");
    expect(has404, "Dashboard shows 404 — routing bug in Kuafor!").toBe(false);
  });

  test("sidebar navigation visible on desktop", async ({ page }) => {
    await login(page);
    // Desktop viewport — sidebar should be visible (not the mobile bottom nav)
    // Look for sidebar links by text content
    const sidebarTexts = ["Randevu", "Personel", "Hizmet", "Kasa", "Ayar"];
    let found = 0;
    for (const text of sidebarTexts) {
      const el = page.locator(`a:has-text("${text}"), button:has-text("${text}")`).first();
      if (await el.isVisible().catch(() => false)) found++;
    }
    expect(found, "Expected sidebar nav items").toBeGreaterThan(0);
  });

  test("randevular page loads", async ({ page }) => {
    await login(page);
    const randevuLink = page.locator('a:has-text("Randevu")').first();
    if (await randevuLink.isVisible().catch(() => false)) {
      await randevuLink.click();
      await page.waitForTimeout(2000);
      const content = await page.content();
      expect(content).not.toContain("Application error");
    }
  });
});
