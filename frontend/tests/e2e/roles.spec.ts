import { expect, test, type Page } from "@playwright/test";

// Hiding a link isn't protection, so these also check the API refuses.
const USERS = {
  owner: "owner-demo-2026",
  manager: "manager-demo-2026",
  staff: "staff-demo-2026",
} as const;
type Role = keyof typeof USERS;

async function login(page: Page, role: Role) {
  await page.goto("/");
  await page.getByLabel(/username/i).fill(role);
  await page.getByLabel(/password/i).fill(USERS[role]);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByTestId("clock-control")).toBeVisible();
  // Some seeded users prefer Arabic. Force English for the labels.
  const r = await page.request.patch("/api/v1/me", {
    data: { language: "en" },
    headers: { "X-CSRF-Token": await csrf(page) },
  });
  expect(r.ok()).toBeTruthy();
  await page.reload();
  await expect(page.getByTestId("clock-control")).toBeVisible();
}

async function navLinks(page: Page): Promise<string[]> {
  const nav = page.getByRole("navigation", { name: /main navigation/i });
  await expect(nav.getByRole("link").first()).toBeVisible();
  return (await nav.getByRole("link").allInnerTexts()).map((s) => s.trim());
}

async function csrf(page: Page): Promise<string> {
  return (await (await page.request.get("/api/v1/me")).json()).csrf_token as string;
}

test.describe.configure({ mode: "serial" });

test("staff see only Stock, cannot open money pages and are refused by the API", async ({ page }) => {
  await login(page, "staff");
  expect(await navLinks(page)).toEqual(["Stock"]);
  for (const path of ["/", "/cash", "/books", "/harness", "/chaos", "/settings"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/stock$/);
  }
  // no costs, no suppliers tab
  await expect(page.getByRole("tab", { name: /suppliers/i })).toHaveCount(0);
  const items = await (await page.request.get("/api/v1/stock/items")).json();
  expect(JSON.stringify(items)).not.toContain("unit_cost");
  for (const url of [
    "/api/v1/home",
    "/api/v1/cash/forecast",
    "/api/v1/reconciliation",
    "/api/v1/vat/summary",
  ]) {
    expect((await page.request.get(url)).status(), url).toBe(403);
  }
  const token = await csrf(page);
  const r = await page.request.patch("/api/v1/settings", {
    data: { values: { price_change_pct: 50 } },
    headers: { "X-CSRF-Token": token },
  });
  expect(r.status()).toBe(403);
});

test("managers see the working views but not Settings or Chaos, and cannot approve rules", async ({
  page,
}) => {
  await login(page, "manager");
  const links = await navLinks(page);
  expect(links).toEqual(expect.arrayContaining(["Home", "Stock", "Cash", "Books", "Harness"]));
  expect(links).not.toContain("Settings");
  expect(links).not.toContain("Chaos mode");
  await page.goto("/settings");
  await expect(page).not.toHaveURL(/\/settings$/);
  const token = await csrf(page);
  const headers = { "X-CSRF-Token": token };
  const rules = (await (await page.request.get("/api/v1/harness/rules")).json()).rules as { id: string }[];
  const target = rules[0]?.id ?? "00000000-0000-0000-0000-000000000000";
  expect([403]).toContain(
    (await page.request.post(`/api/v1/harness/rules/${target}/approve`, { headers })).status(),
  );
  expect(
    (
      await page.request.patch("/api/v1/settings", { data: { values: { price_change_pct: 50 } }, headers })
    ).status(),
  ).toBe(403);
  expect((await page.request.get("/api/v1/users")).status()).toBe(403);
  expect(
    (
      await page.request.post("/api/v1/chaos/inject", { data: { scenario: "price_spike" }, headers })
    ).status(),
  ).toBe(403);
});

test("the owner sees every page, including Settings and Chaos mode", async ({ page }) => {
  await login(page, "owner");
  expect(await navLinks(page)).toEqual(
    expect.arrayContaining(["Home", "Stock", "Cash", "Books", "Harness", "Chaos mode", "Settings"]),
  );
  await page.goto("/settings");
  await expect(page).toHaveURL(/\/settings$/);
  expect((await page.request.get("/api/v1/users")).status()).toBe(200);
});
