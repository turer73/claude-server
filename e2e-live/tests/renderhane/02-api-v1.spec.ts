import { test, expect } from "@playwright/test";

const API_KEY = process.env.RENDERHANE_API_KEY || "";
const BASE = "https://www.renderhane.com/api/v1";

function headers() {
  return {
    Authorization: `Bearer ${API_KEY}`,
    "Content-Type": "application/json",
  };
}

test.describe("Renderhane — API v1", () => {
  test.skip(!API_KEY, "RENDERHANE_API_KEY not set");

  test("GET /balance returns credit count", async ({ request }) => {
    const resp = await request.get(`${BASE}/balance`, { headers: headers() });
    expect(resp.status()).toBe(200);
    const data = await resp.json();
    expect(data).toHaveProperty("balance");
    expect(typeof data.balance).toBe("number");
  });

  test("POST /jobs rejects invalid tool", async ({ request }) => {
    const resp = await request.post(`${BASE}/jobs`, {
      headers: headers(),
      data: { tool: "nonexistent-tool" },
    });
    expect(resp.status()).toBe(400);
    const data = await resp.json();
    expect(data.error).toContain("Invalid tool");
  });

  test("POST /jobs rejects without auth", async ({ request }) => {
    const resp = await request.post(`${BASE}/jobs`, {
      headers: { "Content-Type": "application/json" },
      data: { tool: "text-to-image", prompt: "test" },
    });
    expect(resp.status()).toBe(401);
  });

  test("POST /jobs sync — text-to-image returns output", async ({ request }) => {
    const resp = await request.post(`${BASE}/jobs`, {
      headers: headers(),
      data: { tool: "text-to-image", prompt: "test cat", tier: "fast", sync: true },
    });
    expect(resp.status()).toBe(201);
    const data = await resp.json();
    expect(data.status).toBe("completed");
    expect(data.output?.url).toBeTruthy();
  });

  test("POST /jobs async — submit + poll", async ({ request }) => {
    // Submit
    const submit = await request.post(`${BASE}/jobs`, {
      headers: headers(),
      data: { tool: "text-to-image", prompt: "test dog", tier: "fast" },
    });
    expect(submit.status()).toBe(201);
    const { jobId } = await submit.json();
    expect(jobId).toBeTruthy();

    // Poll until completed (max 60s)
    let output = null;
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      const poll = await request.get(`${BASE}/jobs/${jobId}`, {
        headers: headers(),
      });
      expect(poll.status()).toBe(200);
      const data = await poll.json();
      if (data.status === "completed") {
        output = data.output;
        break;
      }
      if (data.status === "failed") {
        throw new Error(`Job failed: ${data.error}`);
      }
    }
    expect(output).toBeTruthy();
    expect(output?.url).toBeTruthy();
  });

  test("GET /jobs/:id returns 404 for invalid ID", async ({ request }) => {
    const resp = await request.get(`${BASE}/jobs/00000000-0000-0000-0000-000000000000`, {
      headers: headers(),
    });
    expect(resp.status()).toBe(404);
  });
});
