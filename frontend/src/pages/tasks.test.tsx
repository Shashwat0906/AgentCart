import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentTask } from "../lib/types";
import { mockFetch, renderWithProviders } from "../test/utils";
import { TasksPage } from "./TasksPage";

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => {
  localStorage.setItem("agentcart.session", JSON.stringify({
    token: "t", expiresAt: Date.now() + 60_000,
    user: { id: 1, email: "a@example.com", display_name: "Asha", created_at: "2026-01-01T00:00:00Z" },
  }));
});

const CATEGORIES = [{ key: "laptops", label: "Laptops", product_count: 25, min_price_inr: 29990, max_price_inr: 149990, specs: [] }];

function task(over: Partial<AgentTask> = {}): AgentTask {
  return {
    id: "t1", kind: "product_research", goal: "Research laptops under ₹50,000", status: "succeeded",
    progress: 100, current_step: 4, total_steps: 4, input: { category: "laptops", max_price_inr: 50000 },
    result: {
      matches: 4, all_within_budget: true, cart: { item_count: 0, total_inr: 0 },
      top: [{ id: 3, name: "HP 15s", brand: "HP", category: "laptops", price_inr: 36490, rating: 4.2, in_stock: true }],
      best: { price_inr: ["HP 15s"], rating: ["HP 15s"] },
    },
    error: null, cancel_requested: false, attempt: 1, interrupted: false, resumable: false,
    created_at: "2026-10-19T10:00:00Z", updated_at: "2026-10-19T10:00:01Z",
    started_at: "2026-10-19T10:00:00Z", finished_at: "2026-10-19T10:00:01Z", steps: null,
    ...over,
  };
}

function bodies(fetchMock: ReturnType<typeof mockFetch>, suffix: string, method = "POST") {
  return fetchMock.mock.calls
    .filter(([url, init]) => String(url).endsWith(suffix) && (init?.method ?? "GET") === method)
    .map(([, init]) => JSON.parse(String(init?.body ?? "{}")));
}

describe("TasksPage", () => {
  it("lists tasks with status, accessible progress and the research summary", async () => {
    mockFetch((url) => (url.endsWith("/tasks") ? { body: [task()] } : url.endsWith("/categories") ? { body: CATEGORIES } : undefined));
    renderWithProviders(<TasksPage />);
    const card = await screen.findByTestId("task-card");
    expect(within(card).getByText("Research laptops under ₹50,000")).toBeInTheDocument();
    expect(within(card).getByTestId("task-status")).toHaveTextContent("Done");
    expect(within(card).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    const result = within(card).getByTestId("task-result");
    expect(result).toHaveTextContent("4 matching products under ₹50,000");
    expect(within(result).getByRole("link", { name: "HP 15s" })).toHaveAttribute("href", "/products/3");
    expect(within(card).queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
  });

  it("validates the form before sending anything", async () => {
    const fetchMock = mockFetch((url) => (url.endsWith("/tasks") ? { body: [] } : url.endsWith("/categories") ? { body: CATEGORIES } : undefined));
    renderWithProviders(<TasksPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Start research" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a category");
    await userEvent.type(screen.getByLabelText("Looking for"), "laptop");
    await userEvent.type(screen.getByLabelText("Maximum price (₹)"), "50k");
    await userEvent.click(screen.getByRole("button", { name: "Start research" }));
    expect(screen.getByRole("alert")).toHaveTextContent("whole number");
    expect(bodies(fetchMock, "/tasks")).toEqual([]);
  });

  it("starts a typed research task with an idempotency key (never steps or tools)", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/tasks") && init.method === "POST") return { status: 201, body: task({ status: "queued", progress: 0 }) };
      if (url.endsWith("/tasks")) return { body: [] };
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    await screen.findByRole("option", { name: "Laptops" }); // categories load asynchronously
    await userEvent.selectOptions(screen.getByLabelText("Category"), "laptops");
    await userEvent.type(screen.getByLabelText("Maximum price (₹)"), "50000");
    await userEvent.click(screen.getByRole("button", { name: "Start research" }));
    await waitFor(() => expect(bodies(fetchMock, "/tasks")).toHaveLength(1));
    const [body] = bodies(fetchMock, "/tasks");
    expect(body.kind).toBe("product_research");
    expect(body.input).toEqual({ category: "laptops", max_price_inr: 50000, include_cart: true });
    expect(body.idempotency_key).toMatch(/^research-/);
    expect(Object.keys(body).sort()).toEqual(["idempotency_key", "input", "kind"]);
  });

  it("offers Cancel for an active task and Resume only when resumable", async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/tasks/run1/cancel")) return { body: task({ id: "run1", status: "running", cancel_requested: true }) };
      if (url.endsWith("/tasks/fail1/resume")) return { body: task({ id: "fail1", status: "queued" }) };
      if (url.endsWith("/tasks") && (init.method ?? "GET") === "GET")
        return {
          body: [
            task({ id: "run1", goal: "Running task", status: "running", progress: 25, current_step: 1, result: null, finished_at: null }),
            task({ id: "fail1", goal: "Failed task", status: "failed", progress: 25, result: null, resumable: true, error: { code: "temporarily_unavailable", message: "The tool is temporarily unavailable", retryable: true, step: "compare" } }),
            task({ id: "dead1", goal: "Interrupted task", status: "running", interrupted: true, resumable: true, result: null }),
          ],
        };
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    const cards = await screen.findAllByTestId("task-card");
    expect(cards).toHaveLength(3);
    const [running, failed, dead] = cards as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(running).queryByRole("button", { name: "Resume" })).not.toBeInTheDocument();
    expect(within(failed).getByText(/temporary; you can resume/)).toBeInTheDocument();
    expect(within(failed).queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(within(dead).getByTestId("task-status")).toHaveTextContent("Interrupted");

    await userEvent.click(within(running).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(bodies(fetchMock, "/tasks/run1/cancel")).toHaveLength(1));
    await userEvent.click(within(failed).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(bodies(fetchMock, "/tasks/fail1/resume")).toHaveLength(1));
  });

  it("shows each step, including retries and why a step was skipped", async () => {
    mockFetch((url) => {
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/tasks")) return { body: [task()] };
      if (url.endsWith("/tasks/t1"))
        return {
          body: task({
            steps: [
              { position: 0, name: "search", tool: "search_catalog", status: "succeeded", attempts: 3, max_attempts: 3, args: {}, output: {}, error: null, started_at: null, finished_at: null },
              { position: 1, name: "compare", tool: "compare_products", status: "skipped", attempts: 0, max_attempts: 3, args: null, output: { reason: "Only 1 matching product; nothing to compare" }, error: null, started_at: null, finished_at: null },
            ],
          }),
        };
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Show steps" }));
    const found = await screen.findAllByTestId("task-step");
    expect(found).toHaveLength(2);
    const steps = found as [HTMLElement, HTMLElement];
    expect(steps[0]).toHaveTextContent("Search the catalogue");
    expect(steps[0]).toHaveTextContent("3 attempts");
    expect(steps[1]).toHaveTextContent("Only 1 matching product; nothing to compare");
    expect(screen.getByRole("button", { name: "Hide steps" })).toHaveAttribute("aria-expanded", "true");
  });
});
