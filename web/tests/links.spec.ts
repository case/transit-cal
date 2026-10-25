import { expect, test } from "@playwright/test";

const pages = ["/"];

for (const path of pages) {
  test(`external links on ${path} open in a new tab`, async ({ page, baseURL }) => {
    await page.goto(path);
    const origin = new URL(baseURL!).origin;
    const links = await page.locator("a[href]").evaluateAll((els) =>
      els.map((a) => ({
        href: (a as HTMLAnchorElement).href,
        target: a.getAttribute("target"),
      }))
    );
    // Subscribe links are webcal:, which calendar apps handle without a new tab
    const web = links.filter((l) => ["http:", "https:"].includes(new URL(l.href).protocol));
    const external = web.filter((l) => new URL(l.href).origin !== origin);
    expect(external.length).toBeGreaterThan(0);
    for (const link of external) {
      expect(link.target, link.href).toBe("_blank");
    }
  });
}
