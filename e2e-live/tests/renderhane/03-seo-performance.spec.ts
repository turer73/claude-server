import { test, expect } from "@playwright/test";

test.describe("Renderhane — SEO & Performance", () => {
  test("has correct meta tags", async ({ page }) => {
    await page.goto("/tr");
    // Title
    const title = await page.title();
    expect(title.length).toBeGreaterThan(10);

    // Meta description
    const desc = await page.getAttribute('meta[name="description"]', "content");
    expect(desc).toBeTruthy();
    expect(desc!.length).toBeGreaterThan(20);

    // OG tags
    const ogTitle = await page.getAttribute('meta[property="og:title"]', "content");
    expect(ogTitle).toBeTruthy();
  });

  test("robots.txt is accessible", async ({ request }) => {
    const resp = await request.get("https://www.renderhane.com/robots.txt");
    expect(resp.status()).toBe(200);
    const text = await resp.text();
    expect(text).toContain("Sitemap");
  });

  test("sitemap.xml is accessible", async ({ request }) => {
    const resp = await request.get("https://www.renderhane.com/sitemap.xml");
    expect(resp.status()).toBe(200);
    const text = await resp.text();
    expect(text).toContain("<urlset");
  });

  test("manifest.json is valid", async ({ request }) => {
    const resp = await request.get("https://www.renderhane.com/manifest.webmanifest");
    expect(resp.status()).toBe(200);
    const data = await resp.json();
    expect(data.name).toBeTruthy();
  });

  test("landing page loads under 5s", async ({ page }) => {
    const start = Date.now();
    await page.goto("/tr", { waitUntil: "domcontentloaded" });
    const duration = Date.now() - start;
    expect(duration).toBeLessThan(5000);
  });

  test("no broken images on landing", async ({ page }) => {
    await page.goto("/tr");
    // Wait for initial render, not full networkidle (lazy images block it)
    await page.waitForLoadState("domcontentloaded");
    await page.waitForTimeout(2000);
    const images = await page.locator("img").all();
    let broken = 0;
    for (const img of images.slice(0, 15)) {
      const visible = await img.isVisible().catch(() => false);
      if (!visible) continue;
      const src = await img.getAttribute("src");
      if (src && !src.startsWith("data:")) {
        const natural = await img.evaluate(
          (el) => (el as HTMLImageElement).naturalWidth
        );
        if (natural === 0) broken++;
      }
    }
    expect(broken, `${broken} broken images found`).toBe(0);
  });

  test("security headers present", async ({ request }) => {
    const resp = await request.get("https://www.renderhane.com/");
    const headers = resp.headers();
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["strict-transport-security"]).toBeTruthy();
  });
});
