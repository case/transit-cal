import { defineConfig, devices } from "@playwright/test";

// BASE_URL points the tests at a running server; otherwise they start the dev server
const baseURL = process.env.BASE_URL || "http://localhost:8080";

export default defineConfig({
  testDir: "./tests/",
  forbidOnly: !!process.env.CI,
  reporter: "list",
  use: { baseURL },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  ...(!process.env.BASE_URL && {
    webServer: {
      command: "pnpm exec eleventy --serve --port=8080",
      url: "http://localhost:8080/healthz",
      reuseExistingServer: !process.env.CI,
    },
  }),
});
