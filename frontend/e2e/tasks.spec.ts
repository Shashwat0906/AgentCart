import { expect, test } from "@playwright/test";
import { registerViaUI } from "./helpers";

test("research task runs in the background, persists its steps and never touches the cart", async ({ page }) => {
  await registerViaUI(page);
  await page.getByRole("link", { name: "Tasks" }).first().click();
  await expect(page).toHaveURL(/\/tasks$/);
  await expect(page.getByRole("heading", { name: "Research tasks" })).toBeVisible();

  const form = page.getByRole("form", { name: "New research task" });
  await expect(form.getByRole("option", { name: "Laptops" })).toBeAttached(); // categories loaded
  await form.getByLabel("Category").selectOption("laptops");
  await form.getByLabel("Maximum price (₹)").fill("50000");
  await form.getByRole("button", { name: "Start research" }).click();

  const card = page.getByTestId("task-card").first();
  await expect(card).toContainText("Research laptops under ₹50,000");
  await expect(card.getByTestId("task-status")).toHaveText("Done", { timeout: 20_000 });
  await expect(card.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");

  const result = card.getByTestId("task-result");
  await expect(result).toContainText("matching products under ₹50,000");
  const prices = await result.locator("li .tabular-nums").allTextContents();
  expect(prices.length).toBeGreaterThan(0);
  for (const p of prices) expect(Number(p.split("·")[0].replace(/[^\d]/g, ""))).toBeLessThanOrEqual(50_000);

  await card.getByRole("button", { name: "Show steps" }).click();
  const steps = card.getByTestId("task-step");
  await expect(steps).toHaveCount(4);
  await expect(steps.nth(0)).toContainText("Search the catalogue");
  await expect(steps.nth(3)).toContainText("succeeded");

  // The task survives a reload: it is persisted server-side, not held in the page.
  await page.reload();
  await expect(page.getByTestId("task-card").first().getByTestId("task-status")).toHaveText("Done");

  // Read-only: the cart is still empty.
  await page.goto("/cart");
  await expect(page.getByText(/your cart is empty/i)).toBeVisible();
});

test("tasks page requires sign in", async ({ page }) => {
  await page.goto("/tasks");
  await expect(page).toHaveURL(/\/login\?next=%2Ftasks/);
});
