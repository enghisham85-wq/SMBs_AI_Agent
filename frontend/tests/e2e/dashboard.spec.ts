import { expect, test, type Page } from "@playwright/test";

// Runs against the sample cafe seeded by serve-backend.mjs.
const OWNER = { username: "owner", password: "owner-demo-2026" };

async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel(/username/i).fill(OWNER.username);
  await page.getByLabel(/password/i).fill(OWNER.password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByTestId("clock-control")).toBeVisible();
}

async function advanceDays(page: Page, days: number) {
  const token = (await (await page.request.get("/api/v1/me")).json()).csrf_token as string;
  const r = await page.request.post("/api/v1/clock/advance", {
    data: { days },
    headers: { "X-CSRF-Token": token },
  });
  expect(r.ok(), `${r.status()} ${await r.text()}`).toBeTruthy();
}

test.describe.configure({ mode: "serial" });

test("home shows the health strip, decisions and alerts on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await advanceDays(page, 2); // enough for some orders and alerts
  await page.reload();
  const strip = page.getByTestId("health-strip");
  await expect(strip).toBeVisible();
  await expect(strip.getByRole("link")).toHaveCount(4);
  await expect(page.getByTestId("decisions")).toBeVisible();
  await expect(page.getByRole("heading", { name: /alerts/i })).toBeVisible();
  // no horizontal scroll
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.getByRole("button", { name: /^messages$/i }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("a purchase order is approved from Home in one tap", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  const decisions = page.getByTestId("decisions");
  const orders = decisions.getByTestId("approval-card").filter({ hasText: /will run out/i });
  await expect(decisions).toBeVisible();
  for (let i = 0; i < 4 && (await orders.count()) === 0; i++) {
    await advanceDays(page, 1);
    await page.reload();
    await expect(decisions).toBeVisible();
  }
  const before = await orders.count();
  expect(before).toBeGreaterThan(0);
  await orders
    .first()
    .getByRole("button", { name: /^approve$/i })
    .click(); // the single tap
  await expect(orders).toHaveCount(before - 1);
});

test("every figure on Home, Stock, Cash and Books states its freshness", async ({ page }) => {
  await login(page);
  const expectations: [string, number][] = [
    ["/", 5],
    ["/stock", 1],
    ["/cash", 1],
    ["/books", 1],
  ];
  for (const [path, min] of expectations) {
    await page.goto(path);
    await expect(page.getByTestId("freshness").first()).toBeVisible();
    expect(await page.getByTestId("freshness").count(), path).toBeGreaterThanOrEqual(min);
  }
  await page.goto("/");
  const tiles = page.getByTestId("health-strip").getByRole("link");
  for (let i = 0; i < 4; i++) await expect(tiles.nth(i).getByTestId("freshness")).toBeVisible();
});

test("the harness view shows live stages while an action runs", async ({ page }) => {
  await login(page);
  await page.goto("/harness");
  await expect(page.getByRole("tab", { name: /live pipeline/i })).toBeVisible();
  const before = await page
    .getByTestId("pipeline")
    .locator("tbody tr")
    .count()
    .catch(() => 0);
  await advanceDays(page, 1); // streams harness stages
  const rows = page.getByTestId("pipeline").locator("tbody tr");
  await expect.poll(async () => rows.count()).toBeGreaterThan(before);
  await expect(rows.first().getByRole("list", { name: /pipeline/i })).toBeVisible();
  await expect(rows.first().locator(".badge")).toHaveText(/.+/);
});
