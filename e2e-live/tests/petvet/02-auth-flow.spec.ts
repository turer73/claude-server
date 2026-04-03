import { test, expect } from "@playwright/test";

const PHONE = process.env.PETVET_PHONE || "REDACTED_PHONE";
const PASS = process.env.PETVET_PASS || "test1234";
const PIN = process.env.PETVET_PIN || "1234";

async function login(page: import("@playwright/test").Page) {
  await page.goto("/login");

  await page.locator('input[type="tel"], input[type="text"]').first().fill(PHONE);
  await page.locator('input[type="password"]').first().fill(PASS);

  await page.locator(
    'button[type="submit"], button:has-text("Giriş"), button:has-text("Devam")'
  ).first().click();

  // Profile selection — click "Sahip" profile
  const sahipCard = page.locator('text=Sahip').first();
  await sahipCard.waitFor({ timeout: 5000 });
  await sahipCard.click();

  // PIN entry — click numpad digits
  const pinPage = page.locator('text=PIN girin');
  if (await pinPage.isVisible({ timeout: 3000 }).catch(() => false)) {
    for (const digit of PIN) {
      await page.locator(`button:has-text("${digit}")`).first().click();
      await page.waitForTimeout(200);
    }
  }

  // Wait for dashboard to load
  await page.waitForTimeout(2000);
}

test.describe("PetVet — Auth & Dashboard", () => {
  test("full login flow (phone + profile + PIN)", async ({ page }) => {
    await login(page);
    // Should not see PIN or login form
    const content = await page.content();
    const stillOnAuth = content.includes("PIN girin") || content.includes("Kim kullanıyor");
    expect(stillOnAuth, "Still on auth screen").toBe(false);
  });

  test("dashboard has navigation", async ({ page }) => {
    await login(page);
    // Look for any links or navigation elements
    const links = page.locator('a[href]');
    const count = await links.count();
    expect(count).toBeGreaterThan(0);
  });

  test("no application errors", async ({ page }) => {
    await login(page);
    const content = await page.content();
    expect(content).not.toContain("Application error");
    expect(content).not.toContain("500 Internal");
  });
});
