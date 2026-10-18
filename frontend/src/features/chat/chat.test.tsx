import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComparisonTable } from "../../components/ComparisonTable";
import type { AgentAction, ChatMessage, RecommendationCard as Card } from "../../lib/types";
import { mockFetch, renderWithProviders } from "../../test/utils";
import { ApprovalCard } from "./ApprovalCard";
import { MessageView } from "./MessageView";
import { RecommendationCard } from "./RecommendationCard";

afterEach(() => vi.unstubAllGlobals());

const card: Card = {
  product_id: 22, sku: "LAP-022", name: "Lenovo IdeaPad Gaming 3", brand: "Lenovo", category: "laptops",
  price_inr: 61990, mrp_inr: 79990, rating: 4.3, review_count: 4500, stock: 13, rank: 1, score: 59.5,
  breakdown: { budget: 0.56, spec: 0.75, rating: 0.6, preference: 0.5 },
  reasons: ["₹8,010 under your ₹70,000 budget", "RAM: 16 GB ✓"],
  tradeoffs: ["Weaker battery life: 6 h vs up to 12 h"],
  unmet: [], violations: [], is_alternative: false,
  key_specs: [{ key: "ram_gb", label: "RAM", value: "16 GB" }],
};

const action: AgentAction = {
  id: "a-1", kind: "add_to_cart", status: "pending", summary: "Add 1 × Lenovo IdeaPad Gaming 3 (₹61,990 each) to your cart",
  payload: { product_id: 22, quantity: 1, expected_price_inr: 61990 }, result: {}, conversation_id: "c-1",
  created_at: "2026-10-18T10:00:00Z", expires_at: "2026-10-18T10:30:00Z", decided_at: null,
};

describe("RecommendationCard", () => {
  it("shows price, score, reasons and trade-offs from the payload", () => {
    renderWithProviders(<RecommendationCard card={card} />);
    expect(screen.getByRole("heading", { name: "Lenovo IdeaPad Gaming 3" })).toBeInTheDocument();
    expect(screen.getByText("₹61,990")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Match score 60 out of 100" })).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Why it fits" })).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText(/Weaker battery life/)).toBeInTheDocument();
  });

  it("labels alternatives and lists what they miss", () => {
    renderWithProviders(<RecommendationCard card={{ ...card, is_alternative: true, violations: ["₹9,990 over your budget"] }} />);
    expect(screen.getByText(/Alternative — doesn't meet every requirement/)).toBeInTheDocument();
    expect(screen.getByText("₹9,990 over your budget")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /Match score/ })).not.toBeInTheDocument();
  });
});

describe("ApprovalCard", () => {
  it("does nothing until the user approves, then shows the executed state", async () => {
    const fetchMock = mockFetch((url) =>
      url.endsWith("/api/actions/a-1/approve") ? { body: { action: { ...action, status: "executed" }, cart: null } } : undefined,
    );
    renderWithProviders(<ApprovalCard action={action} />);
    expect(screen.getByTestId("action-status")).toHaveTextContent("Waiting for your approval");
    expect(fetchMock).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByTestId("action-status")).toHaveTextContent("Approved & done"));
    expect(fetchMock).toHaveBeenCalledWith("/api/actions/a-1/approve", expect.objectContaining({ method: "POST" }));
  });

  it("asks again when the price changed before approval", async () => {
    const fetchMock = mockFetch((url) => {
      if (url.endsWith("/a-1/approve")) {
        return { status: 409, body: { error: { code: "conflict", message: "The price changed from ₹61,990 to ₹63,490.", details: { reason: "price_changed", new_action_id: "a-2", new_price_inr: 63490 } } } };
      }
      if (url.endsWith("/a-2/approve")) return { body: { action: { ...action, id: "a-2", status: "executed" }, cart: null } };
      return undefined;
    });
    renderWithProviders(<ApprovalCard action={action} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("price changed");
    expect(screen.getByText("₹63,490")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve at new price" }));
    await waitFor(() => expect(screen.getByTestId("action-status")).toHaveTextContent("Approved & done"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("can be rejected", async () => {
    mockFetch((url) => (url.endsWith("/reject") ? { body: { action: { ...action, status: "rejected" }, cart: null } } : undefined));
    renderWithProviders(<ApprovalCard action={action} />);
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(screen.getByTestId("action-status")).toHaveTextContent("Rejected"));
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});

describe("MessageView", () => {
  it("renders summary text safely, chips, notices and the trace", () => {
    const msg: ChatMessage = {
      id: 1, role: "assistant", created_at: "2026-10-18T10:00:00Z",
      content: "My top pick is **Lenovo IdeaPad Gaming 3** <script>alert(1)</script>",
      payload: {
        type: "recommendations", understood: ["Laptops", "RAM ≥ 16 GB (required)"], recommendations: [card],
        notices: ["1 other matching product is currently out of stock and excluded: HP Pavilion Plus 14."],
        security_flags: ["override_instructions"],
        trace: [{ node: "parse_request", status: "ok", ms: 0.4, detail: "" }, { node: "rank_products", status: "ok", ms: 1.2, detail: "10 eligible" }],
        tool_calls: [{ tool: "search_catalog", args: {}, ok: true, attempts: 1, result_count: 25, ms: 1 }],
        provider: "mock",
      },
    };
    const { container } = renderWithProviders(<MessageView message={msg} onQuickReply={() => {}} />);
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByTestId("assistant-summary")).toHaveTextContent("<script>alert(1)</script>");
    expect(screen.getByText("RAM ≥ 16 GB (required)")).toBeInTheDocument();
    expect(screen.getByText(/currently out of stock/)).toBeInTheDocument();
    expect(screen.getByText("Instruction-like text detected")).toBeInTheDocument();
    expect(screen.getByText(/How I got here \(2 steps/)).toBeInTheDocument();
  });

  it("turns clarifying-question options into quick replies", async () => {
    const onQuickReply = vi.fn();
    const msg: ChatMessage = {
      id: 2, role: "assistant", created_at: "2026-10-18T10:00:00Z", content: "I need one detail before I search.",
      payload: { type: "clarification", questions: [{ id: "category", question: "What kind of product are you shopping for?", options: ["Laptops", "Tablets"] }] },
    };
    renderWithProviders(<MessageView message={msg} onQuickReply={onQuickReply} />);
    await userEvent.click(screen.getByRole("button", { name: "Tablets" }));
    expect(onQuickReply).toHaveBeenCalledWith("I'm looking for tablets");
  });
});

describe("ComparisonTable", () => {
  it("marks the best value in each row for screen readers", () => {
    renderWithProviders(
      <ComparisonTable data={{
        columns: [{ product_id: 1, name: "A", brand: "x", category: "laptops" }, { product_id: 2, name: "B", brand: "y", category: "laptops" }],
        rows: [{ key: "battery_hours", label: "Battery life", values: ["11 h", "6 h"], best: [0] }],
      }} />,
    );
    const row = screen.getByRole("row", { name: /Battery life/ });
    expect(within(row).getByText("(best)")).toBeInTheDocument();
    expect(within(row).getAllByRole("cell")[0]).toHaveTextContent("11 h");
  });
});
