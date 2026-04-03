import { test, expect } from "@playwright/test";

const EMAIL = process.env.E2E_EMAIL || "demo@panola.app";
const PASS = process.env.E2E_PASSWORD || "Demo2026!xK9";

test.describe("Panola — Public & Auth", () => {
  test("landing page loads", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("body")).toBeVisible();
    const content = await page.content();
    expect(content).not.toContain("Application error");
  });

  test("login page loads", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator('input[type="email"]').first()).toBeVisible();
  });

  test("login with email + password", async ({ page }) => {
    await page.goto("/login");

    const emailInput = page.locator('input[type="email"]').first();
    await emailInput.fill(EMAIL);

    const passInput = page.locator('input[type="password"]').first();
    await passInput.fill(PASS);

    await page.locator('button[type="submit"]').first().click();

    await page.waitForURL((url) => !url.pathname.includes("/login"), {
      timeout: 15_000,
    });
    expect(page.url()).not.toContain("/login");
  });

  test("dashboard loads after login", async ({ page }) => {
    await page.goto("/login");
    await page.locator('input[type="email"]').first().fill(EMAIL);
    await page.locator('input[type="password"]').first().fill(PASS);
    await page.locator('button[type="submit"]').first().click();
    await page.waitForURL((url) => !url.pathname.includes("/login"), {
      timeout: 15_000,
    });

    await expect(page.locator("main, [role='main'], #root")).toBeVisible();
    const content = await page.content();
    expect(content).not.toContain("Application error");
  });

  test("page loads under 5s", async ({ page }) => {
    const start = Date.now();
    await page.goto("/", { waitUntil: "domcontentloaded" });
    expect(Date.now() - start).toBeLessThan(5000);
  });
});
