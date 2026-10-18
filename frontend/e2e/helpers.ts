import { expect, test, type Page } from "@playwright/test";

/**
 * Sign-up is rate-limited per client IP (RATE_LIMIT_AUTH_PER_MINUTE). Every test registers a fresh
 * account from the same machine, so back-to-back local runs can exhaust the budget. Instead of
 * failing, honour the server's Retry-After once and resubmit; the limit itself stays enforced.
 */
export async function registerViaUI(page: Page, name = "E2E Shopper") {
  const email = `e2e-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
  await page.goto("/register");
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("E2e-shopper-2026");

  const submit = async () => {
    const response = page.waitForResponse((r) => r.url().endsWith("/api/auth/register"));
    await page.getByRole("button", { name: "Create account" }).click();
    return response;
  };
  const first = await submit();
  if (first.status() === 429) {
    const waitSeconds = Math.min(Number(first.headers()["retry-after"] ?? "60") || 60, 60);
    test.info().annotations.push({ type: "rate-limited", description: `sign-up retried after ${waitSeconds}s` });
    test.setTimeout(test.info().timeout + (waitSeconds + 15) * 1000);
    await page.waitForTimeout(waitSeconds * 1000);
    expect((await submit()).status(), "sign-up still rate-limited after Retry-After").toBe(201);
  }
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
