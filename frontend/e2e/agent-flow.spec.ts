import { expect, test } from "@playwright/test";
import { ask, registerViaUI } from "./helpers";

const REFERENCE =
  "I need a laptop under ₹70,000 for machine learning and college work, with 16 GB RAM, good battery life, and upgradeable storage.";

test("recommend → propose → approve → demo checkout", async ({ page }) => {
  await registerViaUI(page);

  const reply = await ask(page, REFERENCE);
  await expect(reply.getByText("What I understood")).toBeVisible();
  await expect(reply.getByText("RAM ≥ 16 GB (required)")).toBeVisible();
  await expect(reply.getByText(/I found \d+ laptops that meet all your requirements/)).toBeVisible();
  const topPick = reply.locator("article h3").first();
  const topName = (await topPick.innerText()).trim();

  const proposal = await ask(page, "add the top pick to my cart");
  await expect(proposal.getByText("The assistant is asking permission")).toBeVisible();
  await expect(proposal.getByTestId("action-status")).toHaveText("Waiting for your approval");
  // Nothing has been added yet.
  await expect(page.getByTestId("cart-count")).toHaveCount(0);

  await proposal.getByRole("button", { name: "Approve" }).click();
  await expect(proposal.getByTestId("action-status")).toHaveText("Approved & done");
  await expect(page.getByTestId("cart-count")).toHaveText("1");

  await page.getByRole("link", { name: /Cart, 1 item/ }).click();
  await expect(page.getByRole("link", { name: topName })).toBeVisible();
  await page.getByRole("link", { name: "Continue to checkout" }).click();

  const place = page.getByRole("button", { name: "Place demo order" });
  await expect(place).toBeDisabled();
  await page.getByRole("checkbox").check();
  await place.click();
  await expect(page.getByRole("heading", { name: "Demo order placed" })).toBeVisible();
  await page.getByRole("link", { name: "View orders" }).click();
  await expect(page.getByText(topName)).toBeVisible();
  await expect(page.getByText("demo payment")).toBeVisible();
});

test("rejecting a proposal leaves the cart untouched; clarifications offer quick replies", async ({ page }) => {
  await registerViaUI(page);
  const clarification = await ask(page, "I want to buy something");
  await expect(clarification.getByText("What kind of product are you shopping for?")).toHaveCount(1);
  await clarification.getByRole("button", { name: "Headphones & Earbuds" }).click();
  await expect(page.getByTestId("assistant-message")).toHaveCount(2);

  const proposal = await ask(page, "add the first one to my cart");
  await proposal.getByRole("button", { name: "Reject" }).click();
  await expect(proposal.getByTestId("action-status")).toHaveText("Rejected");
  await expect(page.getByTestId("cart-count")).toHaveCount(0);
});

test("saved preference requires approval and appears on the preferences page", async ({ page }) => {
  await registerViaUI(page);
  const reply = await ask(page, "remember that I prefer Lenovo");
  await reply.getByRole("button", { name: "Approve" }).click();
  await expect(reply.getByTestId("action-status")).toHaveText("Approved & done");
  await page.getByRole("link", { name: "Preferences" }).first().click();
  await expect(page.getByText("Lenovo", { exact: true })).toBeVisible();
  await expect(page.getByText("Saved via assistant (approved)")).toBeVisible();
});
