import { expect, type Page } from "@playwright/test";

export async function registerViaUI(page: Page, name = "E2E Shopper") {
  const email = `e2e-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
  await page.goto("/register");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("E2e-shopper-2026");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/assistant/);
  return email;
}

export async function ask(page: Page, text: string) {
  const before = await page.getByTestId("assistant-message").count();
  await page.getByLabel("Message the shopping assistant").fill(text);
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByTestId("assistant-message")).toHaveCount(before + 1);
  return page.getByTestId("assistant-message").last();
}
