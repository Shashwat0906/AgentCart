import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProductResearchTask, ShoppingAgentTask } from "../lib/types";
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

function task(over: Partial<ProductResearchTask> = {}): ProductResearchTask {
  return {
    id: "t1", kind: "product_research", goal: "Research laptops under ₹50,000", status: "succeeded",
    progress: 100, current_step: 4, total_steps: 4, input: { category: "laptops", max_price_inr: 50000 },
    result: {
      matches: 4, all_within_budget: true, cart: { item_count: 0, total_inr: 0 },
      top: [{ id: 3, name: "HP 15s", brand: "HP", category: "laptops", price_inr: 36490, rating: 4.2, in_stock: true }],
      best: { price_inr: ["HP 15s"], rating: ["HP 15s"] },
    },
    error: null, cancel_requested: false, attempt: 1, interrupted: false, stalled: false, resumable: false,
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

describe("Task controls", () => {
  const STATES: [string, Partial<ProductResearchTask>, string[]][] = [
    ["queued", { status: "queued", progress: 0, result: null, finished_at: null }, ["Cancel"]],
    ["stalled", { status: "queued", stalled: true, resumable: true, result: null, finished_at: null }, ["Cancel", "Resume"]],
    ["running", { status: "running", progress: 25, result: null, finished_at: null }, ["Cancel"]],
    ["interrupted", { status: "running", interrupted: true, resumable: true, result: null }, ["Cancel", "Resume"]],
    ["succeeded", {}, ["Run again"]],
    ["failed-temp", { status: "failed", resumable: true, result: null, error: { code: "temporarily_unavailable", message: "Temporarily unavailable", retryable: true } }, ["Resume", "Run again"]],
    ["failed-perm", { status: "failed", result: null, error: { code: "not_found", message: "Not found", retryable: false } }, ["Run again"]],
    ["cancelled", { status: "cancelled", result: null }, ["Run again"]],
  ];

  it("shows exactly the actions each state allows", async () => {
    mockFetch((url) =>
      url.endsWith("/tasks") ? { body: STATES.map(([id, over]) => task({ id, goal: id, ...over })) } : url.endsWith("/categories") ? { body: CATEGORIES } : undefined,
    );
    renderWithProviders(<TasksPage />);
    const cards = await screen.findAllByTestId("task-card");
    expect(cards).toHaveLength(STATES.length);
    STATES.forEach(([id, , expected], i) => {
      const card = cards[i] as HTMLElement;
      const actions = within(card).getAllByRole("button").map((b) => b.textContent).filter((n) => n !== "Show steps");
      expect(actions, id).toEqual(expected);
    });
    expect(within(cards[1] as HTMLElement).getByTestId("task-status")).toHaveTextContent("Stalled");
  });

  it("Run again sends only kind, the task's own input and an idempotency key, and shows the new task", async () => {
    const created = task({ id: "new1", goal: "Re-run", status: "queued", progress: 0, result: null, finished_at: null });
    let posted = false;
    const original = task({ id: "old1", input: { query: null, category: "laptops", max_price_inr: 50000, min_rating: null, in_stock: true, compare_top: 3, include_cart: true } as never });
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/tasks") && init.method === "POST") { posted = true; return { status: 201, body: created }; }
      if (url.endsWith("/tasks")) return { body: posted ? [created, original] : [original] };
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Run again" }));
    await waitFor(() => expect(bodies(fetchMock, "/tasks")).toHaveLength(1));
    const [body] = bodies(fetchMock, "/tasks");
    expect(Object.keys(body).sort()).toEqual(["idempotency_key", "input", "kind"]);
    expect(body.kind).toBe("product_research");
    expect(body.input).toEqual({ category: "laptops", max_price_inr: 50000, in_stock: true, compare_top: 3, include_cart: true });
    expect(body.idempotency_key).toMatch(/^rerun-/);
    expect(await screen.findByText("Re-run")).toBeInTheDocument();
    expect(await screen.findByText("Running the task again")).toBeInTheDocument();
  });

  it("a double click on Run again sends one request", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if (url.endsWith("/categories")) return json(200, CATEGORIES);
      if (url.endsWith("/tasks") && init.method === "POST") { await gate; return json(201, task({ id: "new2", status: "queued", result: null })); }
      if (url.endsWith("/tasks")) return json(200, [task()]);
      return json(404, { error: { code: "not_found", message: "Not found", details: {} } });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderWithProviders(<TasksPage />);
    const button = await screen.findByRole("button", { name: "Run again" });
    await userEvent.dblClick(button);
    await userEvent.click(button);
    expect(await screen.findByRole("button", { name: "Starting…" })).toBeDisabled();
    release();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Starting…" })).not.toBeInTheDocument());
    const posts = fetchMock.mock.calls.filter(([u, i]) => String(u).endsWith("/tasks") && i?.method === "POST");
    expect(posts).toHaveLength(1);
  });

  it("after an error the button works again and the retry reuses the same key (no duplicate task)", async () => {
    let attempt = 0;
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/tasks") && init.method === "POST") {
        attempt += 1;
        return attempt === 1
          ? { status: 503, body: { error: { code: "unavailable", message: "Service unavailable", details: {} } } }
          : { status: 201, body: task({ id: "new3", status: "queued", result: null }) };
      }
      if (url.endsWith("/tasks")) return { body: [task()] };
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Run again" }));
    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
    const again = await screen.findByRole("button", { name: "Run again" });
    expect(again).toBeEnabled();
    await userEvent.click(again);
    await waitFor(() => expect(bodies(fetchMock, "/tasks")).toHaveLength(2));
    const [first, second] = bodies(fetchMock, "/tasks");
    expect(second.idempotency_key).toBe(first.idempotency_key);
  });

  it("a stale Cancel (409) shows the error and refreshes the list", async () => {
    let gets = 0;
    mockFetch((url, init) => {
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/tasks/run9/cancel")) return { status: 409, body: { error: { code: "conflict", message: "This task is already succeeded", details: { status: "succeeded" } } } };
      if (url.endsWith("/tasks") && (init.method ?? "GET") === "GET") {
        gets += 1;
        return { body: [gets === 1 ? task({ id: "run9", status: "running", result: null }) : task({ id: "run9" })] };
      }
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("This task is already succeeded")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Run again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("polls while a new task is queued, and not for an interrupted one", async () => {
    const lists = (fetchMock: ReturnType<typeof mockFetch>) =>
      fetchMock.mock.calls.filter(([u, i]) => String(u).endsWith("/tasks") && (i?.method ?? "GET") === "GET").length;
    const queued = mockFetch((url) => (url.endsWith("/tasks") ? { body: [task({ status: "queued", progress: 0, result: null })] } : url.endsWith("/categories") ? { body: CATEGORIES } : undefined));
    const view = renderWithProviders(<TasksPage />);
    await screen.findByTestId("task-card");
    await waitFor(() => expect(lists(queued)).toBeGreaterThanOrEqual(3), { timeout: 5000 });
    view.unmount();
    vi.unstubAllGlobals();

    const dead = mockFetch((url) => (url.endsWith("/tasks") ? { body: [task({ status: "running", interrupted: true, resumable: true, result: null })] } : url.endsWith("/categories") ? { body: CATEGORIES } : undefined));
    renderWithProviders(<TasksPage />);
    await screen.findByTestId("task-card");
    await new Promise((r) => setTimeout(r, 2000));
    expect(lists(dead)).toBe(1);
  }, 12_000);

  it("shows agent tasks without the research summary or Run again", async () => {
    const agent: ShoppingAgentTask = {
      ...task(), id: "a1", kind: "shopping_agent", goal: "Agent: a wireless mouse under ₹3,000",
      input: { goal: "a wireless mouse under ₹3,000" }, current_step: 5, total_steps: 5,
      state: { planner: "rule_based", iterations: 5, tool_calls: 4, invalid_decisions: 0 },
      result: {
        type: "recommendations", question: null, notes: "2 of 6 candidates meet every requirement.",
        products: [{ id: 41, name: "Logi M185", brand: "Logitech", category: "mice", price_inr: 799, rating: 4.3, in_stock: true }],
        requirements: ["Mice", "under ₹3,000"], candidates: 6, eligible: 2,
      },
    };
    mockFetch((url) => (url.endsWith("/tasks") ? { body: [agent] } : url.endsWith("/categories") ? { body: CATEGORIES } : undefined));
    renderWithProviders(<TasksPage />);
    const card = await screen.findByTestId("task-card");
    expect(within(card).getByText("Agent: a wireless mouse under ₹3,000")).toBeInTheDocument();
    expect(within(card).getByTestId("task-status")).toHaveTextContent("Done");
    expect(within(card).queryByTestId("task-result")).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Run again" })).not.toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Show steps" })).toBeInTheDocument();
  });

  it("shows an agent task's cart proposal and approves it only through the approval API", async () => {
    const proposal = {
      id: "act-1", kind: "add_to_cart" as const, status: "pending" as const,
      summary: "Add 1 × Logi M185 (₹799 each) to your cart",
      payload: { product_id: 41, quantity: 1, expected_price_inr: 799, product_name: "Logi M185" }, result: {},
      conversation_id: null, task_id: "a1", created_at: "2026-10-19T10:00:00Z",
      expires_at: "2026-10-19T10:30:00Z", decided_at: null,
    };
    const agent: ShoppingAgentTask = {
      ...task(), id: "a1", kind: "shopping_agent", goal: "Agent: add a wireless mouse under ₹3,000 to my cart",
      input: { goal: "add a wireless mouse under ₹3,000 to my cart", allow_proposals: true },
      current_step: 6, total_steps: 6, result: null, proposals: [proposal],
    };
    const fetchMock = mockFetch((url) => {
      if (url.endsWith("/tasks")) return { body: [agent] };
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/actions/act-1/approve")) {
        return { body: { action: { ...proposal, status: "executed" }, cart: { items: [], item_count: 1, subtotal_inr: 799, shipping_inr: 0, total_inr: 799, problems: [] } } };
      }
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    const card = await screen.findByTestId("task-card");
    const proposals = within(card).getByTestId("task-proposals");
    expect(within(proposals).getByText("Add 1 × Logi M185 (₹799 each) to your cart")).toBeInTheDocument();
    expect(within(proposals).getByTestId("action-status")).toHaveTextContent("Waiting for your approval");
    expect(bodies(fetchMock, "/approve")).toHaveLength(0); // nothing is approved by showing it

    await userEvent.click(within(proposals).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(within(proposals).getByTestId("action-status")).toHaveTextContent("Approved & done"));
    expect(bodies(fetchMock, "/actions/act-1/approve")).toHaveLength(1);
    expect(within(card).queryByRole("button", { name: "Run again" })).not.toBeInTheDocument();
  });

  it("offers no Approve button for an expired task proposal, and shows a budget refusal without a new proposal", async () => {
    const base = {
      kind: "add_to_cart" as const, summary: "Add 1 × Logi M185 (₹799 each) to your cart",
      payload: { product_id: 41, quantity: 1, expected_price_inr: 799, budget_max_inr: 3000 }, result: {},
      conversation_id: null, task_id: "a1", created_at: "2026-10-19T10:00:00Z", expires_at: "2026-10-19T10:30:00Z", decided_at: null,
    };
    const agent: ShoppingAgentTask = {
      ...task(), id: "a1", kind: "shopping_agent", goal: "Agent: add a mouse", input: { goal: "add a mouse", allow_proposals: true },
      result: null, proposals: [{ ...base, id: "old", status: "expired" }, { ...base, id: "act-2", status: "pending" }],
    };
    const message = "The price of Logi M185 is now ₹3,499, outside your budget (up to ₹3,000). Nothing was added to your cart. No new proposal was made.";
    const fetchMock = mockFetch((url) => {
      if (url.endsWith("/tasks")) return { body: [agent] };
      if (url.endsWith("/categories")) return { body: CATEGORIES };
      if (url.endsWith("/actions/act-2/approve")) {
        return { status: 409, body: { error: { code: "conflict", message, details: { reason: "outside_budget", new_price_inr: 3499, budget_max_inr: 3000 } } } };
      }
      return undefined;
    });
    renderWithProviders(<TasksPage />);
    const cards = within(await screen.findByTestId("task-proposals")).getAllByRole("region", { name: "Proposed action" });
    expect(cards).toHaveLength(2);
    const [expired, pending] = cards as [HTMLElement, HTMLElement];
    expect(within(expired).getByTestId("action-status")).toHaveTextContent("Expired");
    expect(within(expired).queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();

    await userEvent.click(within(pending).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(within(pending).getByRole("alert")).toHaveTextContent("outside your budget"));
    expect(within(pending).getByTestId("action-status")).toHaveTextContent("Couldn't complete");
    expect(within(pending).queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument(); // no re-proposal to approve
    expect(bodies(fetchMock, "/approve")).toHaveLength(1);
  });
});
