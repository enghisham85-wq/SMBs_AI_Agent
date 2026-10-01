import { expect, test, type Page } from "@playwright/test";

// T083: the Books page — a sample invoice with its checks next to the original, and a customer invoice
// created in the app with its detail (lines, payments, reminders).
async function login(page: Page) {
  await page.goto("/");
  await page.getByLabel(/username/i).fill("owner");
  await page.getByLabel(/password/i).fill("owner-demo-2026");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByTestId("clock-control")).toBeVisible();
  const token = (await (await page.request.get("/api/v1/me")).json()).csrf_token as string;
  await page.request.patch("/api/v1/me", { data: { language: "en" }, headers: { "X-CSRF-Token": token } });
  await page.reload();
  await expect(page.getByTestId("clock-control")).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test("a sample invoice shows its original, fields with confidence and the checks it passed", async ({
  page,
}) => {
  await login(page);
  await page.goto("/books");
  await page.getByRole("combobox", { name: /sample/i }).selectOption("en_coffee");
  await expect(page.getByRole("status")).toBeVisible({ timeout: 60_000 });
  await page.getByRole("row", { name: /CCR-2026-0412/ }).click();
  await expect(page.locator("iframe, img").first()).toBeVisible(); // the original file
  const checks = page.getByRole("list", { name: "Checks" });
  await expect(checks.getByText("Totals add up")).toBeVisible();
  await expect(checks.getByText("Not a duplicate")).toBeVisible();
});

test("a customer invoice is created with live totals and opens with its payments and reminders", async ({
  page,
}) => {
  await login(page);
  await page.goto("/books");
  await page.getByRole("tab", { name: "Customer invoices" }).click();
  await page.getByPlaceholder("Customer").fill("Garden City Clinic");
  await page.getByPlaceholder("Description").first().fill("Coffee service");
  await page.getByPlaceholder("Price").first().fill("500");
  await expect(page.getByText(/570\.00/).first()).toBeVisible(); // 500 + 14% VAT, computed live
  await page.getByRole("button", { name: "Create invoice" }).click();
  const row = page.getByRole("row", { name: /Garden City Clinic/ });
  await expect(row).toBeVisible();
  await row.click();
  const detail = page.getByTestId("receivable-detail");
  await expect(detail.getByText("Coffee service")).toBeVisible();
  await expect(detail.getByText("Payments")).toBeVisible();
  await expect(detail.getByText("Reminders")).toBeVisible();
});
