import { QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { vi } from "vitest";
import { ToastProvider } from "../components/toast";
import { AuthProvider } from "../lib/auth";
import { makeQueryClient } from "../lib/queryClient";

export function renderWithProviders(ui: ReactElement, { route = "/" } = {}) {
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <ToastProvider>
          <AuthProvider>{ui}</AuthProvider>
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

type Handler = (url: string, init: RequestInit) => { status?: number; body?: unknown } | undefined;

/** Stub global fetch with a router function; unmatched requests return 404. */
export function mockFetch(handler: Handler) {
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input.toString();
    const res = handler(url, init) ?? { status: 404, body: { error: { code: "not_found", message: "Not found", details: {} } } };
    const status = res.status ?? 200;
    return new Response(status === 204 ? null : JSON.stringify(res.body ?? {}), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}
