import { expect, test } from "@playwright/test";

// The 11ty dev server's reload client, through Caddyfile.dev and its CSP
test.skip(!!process.env.BASE_URL, "live reload exists only under bin/run-site");

test("the live reload client connects within the dev CSP", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (msg) => {
    if (msg.text().includes("Content Security Policy")) violations.push(msg.text());
  });
  const socket = page.waitForEvent("websocket");
  await page.goto("/");

  const ws = await socket;
  await expect.poll(() => ws.isClosed()).toBe(false);
  expect(violations).toEqual([]);
});
