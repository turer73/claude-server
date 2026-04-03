import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: [
    ["list"],
    ["json", { outputFile: "results.json" }],
    ["html", { open: "never", outputFolder: "report" }],
  ],
  use: {
    screenshot: "only-on-failure",
    trace: "on-first-retry",
    locale: "tr-TR",
    timezoneId: "Europe/Istanbul",
  },
  projects: [
    {
      name: "renderhane",
      testDir: "./tests/renderhane",
      use: {
        ...devices["Desktop Chrome"],
        baseURL: "https://www.renderhane.com",
      },
    },
    {
      name: "petvet",
      testDir: "./tests/petvet",
      use: {
        ...devices["Desktop Chrome"],
        baseURL: "https://petvet.panola.app",
      },
    },
    {
      name: "kuafor",
      testDir: "./tests/kuafor",
      use: {
        ...devices["Desktop Chrome"],
        baseURL: "https://kuafor.panola.app",
      },
    },
    {
      name: "panola",
      testDir: "./tests/panola",
      use: {
        ...devices["Desktop Chrome"],
        baseURL: "https://panola.app",
      },
    },
  ],
});
