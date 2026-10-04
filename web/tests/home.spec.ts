import { expect, test } from "@playwright/test";

test("home page renders with header, footer and styles", async ({ page }) => {
  const css = page.waitForResponse("**/css/pico.min.css");
  await page.goto("/");

  await expect(page).toHaveTitle("transit-cal");
  await expect(page.getByRole("link", { name: "transit-cal" })).toHaveAttribute("href", "/");
  await expect(page.getByRole("contentinfo")).toContainText("511.org");
  await expect(page.getByRole("contentinfo")).toContainText("Transitland");
  expect((await css).status()).toBe(200);
});

test("healthz returns OK", async ({ request }) => {
  const res = await request.get("/healthz");
  expect(res.status()).toBe(200);
  expect(await res.text()).toContain("200 OK");
});
