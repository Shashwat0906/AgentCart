import type {
  AgentAction,
  AgentTask,
  ApprovalResponse,
  Cart,
  Category,
  ChatResponse,
  Comparison,
  ConversationDetail,
  ConversationSummary,
  Order,
  Preference,
  PreferenceKind,
  ProductDetail,
  ProductPage,
  ProductResearchInput,
  PublicConfig,
  Quote,
  TokenResponse,
  User,
  Weights,
} from "./types";

const BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

/** Error carrying the backend's structured `{error: {code, message, details}}` body. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

let getToken: () => string | null = () => null;
let onUnauthorized: () => void = () => {};

export function configureApi(opts: { getToken: () => string | null; onUnauthorized: () => void }) {
  getToken = opts.getToken;
  onUnauthorized = opts.onUnauthorized;
}

type Query = Record<string, string | number | boolean | string[] | undefined | null>;

function toQuery(params: Query = {}): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => qs.append(k, x));
    else qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

export async function request<T>(
  path: string,
  init: { method?: string; body?: unknown; query?: Query; signal?: AbortSignal } = {},
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (init.body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(`${BASE}/api${path}${toQuery(init.query)}`, {
      method: init.method ?? "GET",
      headers,
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      signal: init.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "Can't reach the AgentCart server. Is the backend running?");
  }

  if (res.status === 204) return undefined as T;
  const data: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const err = (data as { error?: { code?: string; message?: string; details?: Record<string, unknown> } } | null)?.error;
    if (res.status === 401 && token) onUnauthorized();
    throw new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `Request failed (${res.status})`, err?.details ?? {});
  }
  return data as T;
}

export interface ProductQuery {
  q?: string;
  category?: string;
  brand?: string[];
  min_price?: number;
  max_price?: number;
  min_rating?: number;
  in_stock?: boolean;
  sort?: "relevance" | "price_asc" | "price_desc" | "rating" | "newest" | "name";
  page?: number;
  page_size?: number;
}

export const api = {
  // catalogue
  products: (q: ProductQuery, signal?: AbortSignal) => request<ProductPage>("/products", { query: q as Query, signal }),
  product: (id: number) => request<ProductDetail>(`/products/${id}`),
  compare: (ids: number[]) => request<Comparison>("/products/compare", { query: { ids: ids.join(",") } }),
  categories: () => request<Category[]>("/categories"),
  brands: (category?: string) => request<string[]>("/brands", { query: { category } }),
  publicConfig: () => request<PublicConfig>("/config/public"),

  // auth
  register: (body: { email: string; password: string; display_name: string }) =>
    request<TokenResponse>("/auth/register", { method: "POST", body }),
  login: (body: { email: string; password: string }) => request<TokenResponse>("/auth/login", { method: "POST", body }),
  me: () => request<User>("/auth/me"),

  // cart
  cart: () => request<Cart>("/cart"),
  addToCart: (product_id: number, quantity: number) =>
    request<Cart>("/cart/items", { method: "POST", body: { product_id, quantity } }),
  setQuantity: (product_id: number, quantity: number) =>
    request<Cart>(`/cart/items/${product_id}`, { method: "PATCH", body: { quantity } }),
  removeFromCart: (product_id: number) => request<Cart>(`/cart/items/${product_id}`, { method: "DELETE" }),
  clearCart: () => request<Cart>("/cart", { method: "DELETE" }),

  // agent
  chat: (body: { message: string; conversation_id?: string; weights?: Weights }) =>
    request<ChatResponse>("/chat", { method: "POST", body }),
  conversations: () => request<ConversationSummary[]>("/conversations"),
  conversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),
  deleteConversation: (id: string) => request<void>(`/conversations/${id}`, { method: "DELETE" }),
  actions: (status?: string) => request<AgentAction[]>("/actions", { query: { status } }),
  approve: (id: string) => request<ApprovalResponse>(`/actions/${id}/approve`, { method: "POST" }),
  reject: (id: string) => request<ApprovalResponse>(`/actions/${id}/reject`, { method: "POST" }),

  // preferences
  preferences: () => request<Preference[]>("/preferences"),
  createPreference: (body: { kind: PreferenceKind; value: string; category?: string | null }) =>
    request<Preference>("/preferences", { method: "POST", body }),
  updatePreference: (id: number, body: { value?: string; category?: string | null }) =>
    request<Preference>(`/preferences/${id}`, { method: "PATCH", body }),
  deletePreference: (id: number) => request<void>(`/preferences/${id}`, { method: "DELETE" }),

  // checkout
  quote: () => request<Quote>("/checkout/quote", { method: "POST" }),
  confirm: (quote_id: string, idempotency_key: string) =>
    request<Order>("/checkout/confirm", { method: "POST", body: { quote_id, confirm: true, idempotency_key } }),
  orders: () => request<Order[]>("/orders"),
  order: (id: number) => request<Order>(`/orders/${id}`),

  // persistent agent tasks (read-only research; nothing is bought or added to the cart)
  tasks: () => request<AgentTask[]>("/tasks"),
  task: (id: string) => request<AgentTask>(`/tasks/${id}`),
  createTask: (input: ProductResearchInput, idempotency_key?: string) =>
    request<AgentTask>("/tasks", { method: "POST", body: { kind: "product_research", input, idempotency_key } }),
  cancelTask: (id: string) => request<AgentTask>(`/tasks/${id}/cancel`, { method: "POST" }),
  resumeTask: (id: string) => request<AgentTask>(`/tasks/${id}/resume`, { method: "POST" }),
};
