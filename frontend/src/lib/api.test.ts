import { afterEach, describe, expect, it, vi } from "vitest";
import { mockFetch } from "../test/utils";
import { api, ApiError, configureApi } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
  configureApi({ getToken: () => null, onUnauthorized: () => {} });
});

describe("api client", () => {
  it("sends the bearer token and JSON body", async () => {
    const fetchMock = mockFetch(() => ({ body: { items: [], item_count: 0 } }));
    configureApi({ getToken: () => "tok-123", onUnauthorized: () => {} });
    await api.addToCart(5, 2);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/cart/items");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer tok-123");
    expect(JSON.parse(init?.body as string)).toEqual({ product_id: 5, quantity: 2 });
  });

  it("builds query strings with repeated keys and skips empty values", async () => {
    const fetchMock = mockFetch(() => ({ body: { items: [], total: 0, page: 1, page_size: 12, pages: 1 } }));
    await api.products({ q: "laptop", brand: ["HP", "Dell"], category: undefined, in_stock: true });
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/products?q=laptop&brand=HP&brand=Dell&in_stock=true");
  });

  it("turns structured error bodies into ApiError", async () => {
    mockFetch(() => ({ status: 409, body: { error: { code: "conflict", message: "Only 3 left", details: { available: 3 } } } }));
    const err = await api.addToCart(1, 9).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 409, code: "conflict", message: "Only 3 left", details: { available: 3 } });
  });

  it("calls onUnauthorized only when a token was sent", async () => {
    const onUnauthorized = vi.fn();
    mockFetch(() => ({ status: 401, body: { error: { code: "unauthorized", message: "Session expired", details: {} } } }));
    configureApi({ getToken: () => null, onUnauthorized });
    await api.cart().catch(() => {});
    expect(onUnauthorized).not.toHaveBeenCalled();
    configureApi({ getToken: () => "expired", onUnauthorized });
    await api.cart().catch(() => {});
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("reports network failures clearly", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(api.categories()).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it("handles 204 responses", async () => {
    mockFetch(() => ({ status: 204 }));
    await expect(api.deletePreference(1)).resolves.toBeUndefined();
  });
});
