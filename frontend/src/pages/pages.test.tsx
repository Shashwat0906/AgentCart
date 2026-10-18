import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RequireAuth } from "../components/Layout";
import { mockFetch, renderWithProviders } from "../test/utils";
import { LoginPage, RegisterPage } from "./AuthPages";
import { CartPage } from "./CartPage";

afterEach(() => vi.unstubAllGlobals());

function Where() {
  const loc = useLocation();
  return <p data-testid="where">{loc.pathname + loc.search}</p>;
}

function signedIn() {
  localStorage.setItem("agentcart.session", JSON.stringify({
    token: "t", expiresAt: Date.now() + 60_000,
    user: { id: 1, email: "a@example.com", display_name: "Asha", created_at: "2026-01-01T00:00:00Z" },
  }));
}

describe("RequireAuth", () => {
  it("redirects anonymous users to sign in and remembers where they were going", () => {
    renderWithProviders(
      <Routes>
        <Route path="/cart" element={<RequireAuth><p>secret cart</p></RequireAuth>} />
        <Route path="/login" element={<Where />} />
      </Routes>,
      { route: "/cart" },
    );
    expect(screen.queryByText("secret cart")).not.toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("/login?next=%2Fcart");
  });
});

describe("LoginPage", () => {
  it("ignores off-site ?next= targets (no open redirect)", async () => {
    mockFetch((url) =>
      url.endsWith("/auth/login")
        ? { body: { access_token: "tok", token_type: "bearer", expires_in: 3600, user: { id: 1, email: "a@example.com", display_name: "A", created_at: "" } } }
        : undefined,
    );
    renderWithProviders(
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Where />} />
      </Routes>,
      { route: "/login?next=//evil.example.com" },
    );
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "Passw0rd!x");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/assistant"));
  });

  it("shows the server's error message", async () => {
    mockFetch(() => ({ status: 401, body: { error: { code: "unauthorized", message: "Incorrect email or password", details: {} } } }));
    renderWithProviders(<LoginPage />, { route: "/login" });
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Incorrect email or password")).toBeInTheDocument();
  });
});

describe("RegisterPage", () => {
  it("blocks weak passwords before calling the API", async () => {
    const fetchMock = mockFetch(() => undefined);
    renderWithProviders(<RegisterPage />, { route: "/register" });
    await userEvent.type(screen.getByLabelText("Name"), "Asha");
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "short");
    expect(screen.getByRole("button", { name: "Create account" })).toBeDisabled();
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("CartPage", () => {
  it("renders totals computed by the server", async () => {
    signedIn();
    mockFetch((url) =>
      url.endsWith("/api/cart")
        ? { body: {
            items: [{ product_id: 1, sku: "LAP-001", name: "Lenovo IdeaPad Slim 5", brand: "Lenovo", category: "laptops", unit_price_inr: 62990, mrp_inr: 79990, quantity: 2, line_total_inr: 125980, stock: 18, problem: null, key_specs: [] }],
            item_count: 2, subtotal_inr: 125980, shipping_inr: 0, total_inr: 125980, free_shipping_threshold_inr: 999, max_item_quantity: 10, issues: [],
          } }
        : url.endsWith("/auth/me") ? { body: { id: 1, email: "a@example.com", display_name: "Asha", created_at: "" } } : undefined,
    );
    renderWithProviders(<CartPage />, { route: "/cart" });
    expect(await screen.findByText("Lenovo IdeaPad Slim 5")).toBeInTheDocument();
    expect(screen.getByTestId("subtotal")).toHaveTextContent("₹1,25,980");
    expect(screen.getByTestId("total")).toHaveTextContent("₹1,25,980");
    expect(screen.getByRole("link", { name: "Continue to checkout" })).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    signedIn();
    mockFetch((url) => (url.endsWith("/api/cart") ? { body: { items: [], item_count: 0, subtotal_inr: 0, shipping_inr: 0, total_inr: 0, free_shipping_threshold_inr: 999, max_item_quantity: 10, issues: [] } } : undefined));
    renderWithProviders(<CartPage />, { route: "/cart" });
    expect(await screen.findByText("Your cart is empty")).toBeInTheDocument();
  });
});
