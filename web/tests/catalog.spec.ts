import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

// The catalog JSON bin/run-site wrote, and the origin it gave the site's feed links
test.skip(!!process.env.BASE_URL, "compares against the catalog bin/run-site wrote");
const origin = "http://127.0.0.1:8090";

// Read inside each test, so a skipped run never needs the generated file
function load() {
  const catalog = JSON.parse(readFileSync(new URL("../source/_data/catalog.json", import.meta.url), "utf8"));
  const feeds: { name: string; path: string }[] = catalog.operators.flatMap((op: any) => op.routes.flatMap((route: any) => route.feeds));
  return { catalog, feeds };
}

test("the home page lists every catalog feed once, with webcal and http URLs", async ({ page }) => {
  const { feeds } = load();
  await page.goto("/");
  const webcal = page.locator("a[href^=\"webcal:\"]");

  await expect(webcal).toHaveCount(feeds.length);
  for (const feed of feeds) {
    const link = page.locator(`a[href="webcal://127.0.0.1:8090/${feed.path}"]`);
    await expect(link).toHaveCount(1);
    await expect(link).toHaveText(feed.name);
    await expect(link.locator("xpath=following-sibling::code")).toHaveText(`${origin}/${feed.path}`);
  }
});

test("each operator and route links to the operator's own pages", async ({ page }) => {
  const { catalog } = load();
  await page.goto("/");
  for (const op of catalog.operators) {
    await expect(page.getByRole("link", { name: op.name, exact: true })).toHaveAttribute("href", op.website);
    for (const route of op.routes) {
      await expect(page.getByRole("link", { name: route.name, exact: true })).toHaveAttribute("href", route.url);
    }
  }
});
