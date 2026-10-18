// Types mirroring the FastAPI response models (backend/app/schemas).

export interface Spec {
  key: string;
  label: string;
  value: string;
}

export interface Product {
  id: number;
  sku: string;
  name: string;
  brand: string;
  category: string;
  price_inr: number;
  mrp_inr: number;
  rating: number;
  review_count: number;
  stock: number;
  in_stock: boolean;
  tags: string[];
  key_specs: Spec[];
}

export interface ProductDetail extends Product {
  description: string;
  specs: Spec[];
  raw_specs: Record<string, unknown>;
}

export interface ProductPage {
  items: Product[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface Category {
  key: string;
  label: string;
  product_count: number;
  min_price_inr: number;
  max_price_inr: number;
}

export interface ComparisonRow {
  key: string;
  label: string;
  values: string[];
  best: number[];
}

export interface Comparison {
  columns: { product_id: number; name: string; brand: string; category: string }[];
  rows: ComparisonRow[];
  products?: Product[];
}

export interface User {
  id: number;
  email: string;
  display_name: string;
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: User;
}

export interface CartItem {
  product_id: number;
  sku: string;
  name: string;
  brand: string;
  category: string;
  unit_price_inr: number;
  mrp_inr: number;
  quantity: number;
  line_total_inr: number;
  stock: number;
  problem: string | null;
  key_specs: Spec[];
}

export interface Cart {
  items: CartItem[];
  item_count: number;
  subtotal_inr: number;
  shipping_inr: number;
  total_inr: number;
  free_shipping_threshold_inr: number;
  max_item_quantity: number;
  issues: string[];
}

export type Weights = { budget: number; spec: number; rating: number; preference: number };

export interface RecommendationCard {
  product_id: number;
  sku: string;
  name: string;
  brand: string;
  category: string;
  price_inr: number;
  mrp_inr: number;
  rating: number;
  review_count: number;
  stock: number;
  rank: number;
  score: number;
  breakdown: Weights;
  reasons: string[];
  tradeoffs: string[];
  unmet: string[];
  violations: string[];
  is_alternative: boolean;
  key_specs: Spec[];
}

export interface TraceStep {
  node: string;
  status: "ok" | "error" | "skipped";
  ms: number;
  detail: string;
}

export interface ToolCall {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  attempts: number;
  result_count: number | null;
  ms: number;
}

export interface Question {
  id: string;
  question: string;
  options: string[];
}

export type ActionStatus = "pending" | "executed" | "rejected" | "failed" | "expired" | "superseded";

export interface AgentAction {
  id: string;
  kind: "add_to_cart" | "save_preference";
  status: ActionStatus;
  summary: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  conversation_id: string | null;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
}

export type ResponseType =
  | "recommendations"
  | "no_results"
  | "clarification"
  | "comparison"
  | "cart_proposal"
  | "preference_proposal"
  | "help"
  | "reset"
  | "error";

export interface AgentPayload {
  type?: ResponseType;
  understood?: string[];
  recommendations?: RecommendationCard[];
  alternatives?: RecommendationCard[];
  comparison?: Comparison | null;
  questions?: Question[];
  notices?: string[];
  applied_preferences?: string[];
  security_flags?: string[];
  trace?: TraceStep[];
  tool_calls?: ToolCall[];
  provider?: string;
  llm_used?: boolean;
  weights?: Weights;
  awaiting_approval?: boolean;
  action_ids?: string[];
  actions?: AgentAction[];
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  payload: AgentPayload;
  created_at: string;
}

export interface ChatResponse {
  conversation_id: string;
  title: string;
  user_message: ChatMessage;
  assistant_message: ChatMessage;
  actions: AgentAction[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
}

export interface ConversationDetail {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: ChatMessage[];
}

export type PreferenceKind = "preferred_brand" | "avoided_brand" | "budget_max" | "use_case" | "note";

export interface Preference {
  id: number;
  kind: PreferenceKind;
  value: string;
  category: string | null;
  source: "user" | "agent";
  created_at: string;
  updated_at: string;
}

export interface Quote {
  quote_id: string;
  expires_at: string;
  items: CartItem[];
  subtotal_inr: number;
  shipping_inr: number;
  total_inr: number;
  payment_mode: "demo";
}

export interface OrderItem {
  product_id: number | null;
  sku: string;
  name: string;
  unit_price_inr: number;
  quantity: number;
  line_total_inr: number;
}

export interface Order {
  id: number;
  status: string;
  payment_mode: string;
  subtotal_inr: number;
  shipping_inr: number;
  total_inr: number;
  created_at: string;
  items: OrderItem[];
}

export interface ApprovalResponse {
  action: AgentAction;
  cart: Cart | null;
}

export interface PublicConfig {
  llm_provider: string;
  ranking_weights: Weights;
  max_item_quantity: number;
  free_shipping_threshold_inr: number;
  shipping_fee_inr: number;
  payment_mode: "demo";
}
