import { expect, test } from "@playwright/test";

test("guest can browse, search, filter and open a product", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Say what you need");
  await page.goto("/products");
  await page.getByLabel("Search products").fill("noise cancelling");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Results for “noise cancelling”");
  await expect(page.getByRole("region", { name: "Products" })).toHaveAttribute("aria-busy", "false");
  const first = page.locator("article h3 a").first();
  const name = await first.innerText();
  await first.click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(name);
  await expect(page.getByRole("heading", { name: "Specifications" })).toBeVisible();
});

test("protected pages redirect to sign in", async ({ page }) => {
  await page.goto("/cart");
  await expect(page).toHaveURL(/\/login\?next=%2Fcart/);
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
});
