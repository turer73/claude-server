import { test, expect } from "@playwright/test";

test.describe("PetVet — Public Pages", () => {
  test("landing page loads", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator("body")).toBeVisible();
    const content = await page.content();
    expect(content).not.toContain("Application error");
  });

  test("login page loads", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator('input[type="tel"], input[type="text"]').first()).toBeVisible();
    await expect(page.locator('input[type="password"]').first()).toBeVisible();
  });

  test("page loads under 5s", async ({ page }) => {
    const start = Date.now();
    await page.goto("/", { waitUntil: "domcontentloaded" });
    expect(Date.now() - start).toBeLessThan(5000);
  });
});
