import { defineConfig, devices } from "@playwright/test";

// BASE_URL points the tests at a running server; otherwise they start bin/run-site on their own
// ports, serving the fixture feeds, so a dev server already on 8080 is never reused
const baseURL = process.env.BASE_URL || "http://localhost:8090";

export default defineConfig({
  testDir: "./tests/",
  forbidOnly: !!process.env.CI,
  reporter: "list",
  use: { baseURL },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  ...(!process.env.BASE_URL && {
    webServer: {
      command: "../bin/run-site",
      url: "http://localhost:8090/healthz",
      // The space proves Caddyfile.dev quotes the feeds path
      env: { PORT: "8090", ELEVENTY_PORT: "8091", FEEDS: "tests/fixtures/feed root" },
      reuseExistingServer: false,
    },
  }),
});
