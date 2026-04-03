import { test, expect } from "@playwright/test";

test.describe("Renderhane — Public Pages", () => {
  test("landing page loads", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/renderhane/i);
    // Hero section visible
    await expect(page.locator("main")).toBeVisible();
    // No server error
    const content = await page.content();
    expect(content).not.toContain("Application error");
  });

  test("login page loads", async ({ page }) => {
    await page.goto("/tr/login");
    await expect(page.locator('input[type="email"]')).toBeVisible();
  });

  test("terms page loads", async ({ page }) => {
    const resp = await page.goto("/tr/terms");
    expect(resp?.status()).toBeLessThan(400);
    await expect(page.locator("main")).toBeVisible();
  });

  test("privacy page loads", async ({ page }) => {
    await page.goto("/tr/privacy");
    await expect(page.locator("main")).toBeVisible();
  });

  test("blog page loads", async ({ page }) => {
    await page.goto("/tr/blog");
    await expect(page.locator("main")).toBeVisible();
  });

  test("KVKK page loads", async ({ page }) => {
    await page.goto("/tr/kvkk");
    await expect(page.locator("main")).toBeVisible();
  });

  test("cookie policy page loads", async ({ page }) => {
    await page.goto("/tr/cookie-policy");
    await expect(page.locator("main")).toBeVisible();
  });

  test("English locale works", async ({ page }) => {
    await page.goto("/en");
    await expect(page).toHaveTitle(/renderhane/i);
  });

  test("unauthenticated /app redirects to login", async ({ page }) => {
    await page.goto("/tr/app");
    await page.waitForURL(/login/, { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });
});
