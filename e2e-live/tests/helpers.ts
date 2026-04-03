import { Page, expect } from "@playwright/test";

/** Wait for page to be fully loaded (no network activity for 500ms) */
export async function waitForLoad(page: Page) {
  await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
}

/** Login via phone + password form (PetVet / Kuafor pattern) */
export async function loginWithPhone(
  page: Page,
  phone: string,
  password: string
) {
  await page.goto("/login");
  await waitForLoad(page);

  // Try common phone input patterns
  const phoneInput =
    page.getByPlaceholder(/telefon|phone|cep/i).first() ||
    page.locator('input[type="tel"]').first() ||
    page.locator('input[name*="phone"]').first();
  await phoneInput.fill(phone);

  const passInput =
    page.getByPlaceholder(/şifre|parola|password/i).first() ||
    page.locator('input[type="password"]').first();
  await passInput.fill(password);

  // Click submit
  const submitBtn =
    page.getByRole("button", { name: /giriş|login|devam/i }).first();
  await submitBtn.click();

  // Wait for navigation away from login
  await page.waitForURL((url) => !url.pathname.includes("/login"), {
    timeout: 10_000,
  });
}

/** Login via email + password (Renderhane / Panola pattern) */
export async function loginWithEmail(
  page: Page,
  email: string,
  password: string
) {
  await page.goto("/login");
  await waitForLoad(page);

  const emailInput =
    page.getByPlaceholder(/e-posta|email/i).first() ||
    page.locator('input[type="email"]').first();
  await emailInput.fill(email);

  const passInput =
    page.getByPlaceholder(/şifre|parola|password/i).first() ||
    page.locator('input[type="password"]').first();
  await passInput.fill(password);

  const submitBtn =
    page.getByRole("button", { name: /giriş|login|devam|sign in/i }).first();
  await submitBtn.click();

  await page.waitForURL((url) => !url.pathname.includes("/login"), {
    timeout: 10_000,
  });
}

/** Check page loads without errors */
export async function expectPageOk(page: Page) {
  // No crash / error page
  const content = await page.content();
  expect(content).not.toContain("500 Internal Server Error");
  expect(content).not.toContain("Application error");
}

/** Check for console errors (collects during test) */
export function collectConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      const text = msg.text();
      // Ignore common non-critical errors
      if (
        !text.includes("favicon") &&
        !text.includes("third-party") &&
        !text.includes("analytics")
      ) {
        errors.push(text);
      }
    }
  });
  return errors;
}

/** Check API health endpoint */
export async function checkApiHealth(page: Page, path: string) {
  const response = await page.request.get(path);
  expect(response.status()).toBeLessThan(500);
  return response;
}
