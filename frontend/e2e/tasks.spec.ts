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

  // A completed task offers only "Run again" (Cancel and Resume don't apply).
  await expect(card.getByRole("button", { name: "Cancel" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Resume" })).toHaveCount(0);
  await card.getByRole("button", { name: "Run again" }).dblclick(); // a double click must not create two tasks
  const cards = page.getByTestId("task-card");
  await expect(cards).toHaveCount(2);
  await expect(cards.first().getByTestId("task-status")).toHaveText("Done", { timeout: 20_000 }); // polled to completion
  await expect(cards.first()).toContainText("Research laptops under ₹50,000");
  await expect(cards.nth(1).getByTestId("task-status")).toHaveText("Done"); // the original is untouched

  // Tasks survive a reload: they are persisted server-side, not held in the page.
  await page.reload();
  await expect(page.getByTestId("task-card")).toHaveCount(2);
  await expect(page.getByTestId("task-card").first().getByTestId("task-status")).toHaveText("Done");

  // Read-only: the cart is still empty.
  await page.goto("/cart");
  await expect(page.getByText(/your cart is empty/i)).toBeVisible();
});

test("laptop research under ₹60,000 finds the catalogue's laptops even with the budget typed as text", async ({ page }) => {
  await registerViaUI(page);
  await page.goto("/tasks");
  const form = page.getByRole("form", { name: "New research task" });
  await expect(form.getByRole("option", { name: "Laptops" })).toBeAttached();
  await form.getByLabel("Category").selectOption("laptops");
  await form.getByLabel("Looking for").fill("laptop under ₹60,000");
  await form.getByLabel("Maximum price (₹)").fill("60000");
  await form.getByRole("button", { name: "Start research" }).click();

  const card = page.getByTestId("task-card").first();
  await expect(card.getByTestId("task-status")).toHaveText("Done", { timeout: 20_000 });
  const result = card.getByTestId("task-result");
  await expect(result).toContainText("7 matching products under ₹60,000"); // was "0 matching products"
  const prices = await result.locator("li .tabular-nums").allTextContents();
  expect(prices.length).toBeGreaterThan(0);
  for (const p of prices) expect(Number(p.split("·")[0].replace(/[^\d]/g, ""))).toBeLessThanOrEqual(60_000);

  // A typed minimum above the maximum can't be satisfied: refused, not silently dropped.
  await form.getByLabel("Looking for").fill("laptop above ₹60,000");
  await form.getByLabel("Maximum price (₹)").fill("50000");
  await form.getByRole("button", { name: "Start research" }).click();
  await expect(page.getByText("The price in “Looking for” conflicts with the maximum price")).toBeVisible();
  await expect(page.getByTestId("task-card")).toHaveCount(1); // no task was created
});

test("a sentence with a typed budget ('I need a laptop under ₹60,000') finds laptops within it", async ({ page }) => {
  // Production report after PR #16: this exact input completed 4/4 with "0 matching products".
  await registerViaUI(page);
  await page.goto("/tasks");
  const form = page.getByRole("form", { name: "New research task" });
  await expect(form.getByRole("option", { name: "Laptops" })).toBeAttached();
  await form.getByLabel("Category").selectOption("laptops");
  await form.getByLabel("Looking for").fill("I need a laptop under ₹60,000");
  await form.getByLabel("Maximum price (₹)").fill("60000");
  await form.getByRole("button", { name: "Start research" }).click();

  const card = page.getByTestId("task-card").first();
  await expect(card.getByTestId("task-status")).toHaveText("Done", { timeout: 20_000 });
  const result = card.getByTestId("task-result");
  await expect(result).toContainText("7 matching products under ₹60,000");
  const prices = await result.locator("li .tabular-nums").allTextContents();
  expect(prices.length).toBeGreaterThan(0);
  for (const p of prices) expect(Number(p.split("·")[0].replace(/[^\d]/g, ""))).toBeLessThanOrEqual(60_000);
});

test("tasks page requires sign in", async ({ page }) => {
  await page.goto("/tasks");
  await expect(page).toHaveURL(/\/login\?next=%2Ftasks/);
});
