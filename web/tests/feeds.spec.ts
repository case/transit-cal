import { expect, test } from "@playwright/test";

// Served by bin/run-site from web/tests/fixtures/feed root, through Caddyfile.dev's feed rule
test.skip(!!process.env.BASE_URL, "needs the fixture feeds that bin/run-site serves");

test("a published feed path is served as a calendar", async ({ request }) => {
  const res = await request.get("/o-test-ferry/oak-to-hub.ics");
  expect(res.status()).toBe(200);
  expect(res.headers()["content-type"]).toMatch(/^text\/calendar/);
  expect(await res.text()).toContain("BEGIN:VCALENDAR");
});

for (const path of ["/current/o-test-ferry/oak-to-hub.ics", "/gtfs/abc.zip"]) {
  test(`${path} outside the feed rule is not served`, async ({ request }) => {
    expect((await request.get(path)).status()).toBe(404);
  });
}
