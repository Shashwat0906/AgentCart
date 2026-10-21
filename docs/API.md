# API reference

Base path `/api`. Interactive docs are at `/api/docs` when `ENVIRONMENT` is not `production`.

* **Auth:** `Authorization: Bearer <access_token>` from `/auth/register` or `/auth/login`. Rows marked 🔒 require it.
* **Errors** always look like this:

  ```json
  { "error": { "code": "conflict", "message": "Only 3 unit(s) of … available", "details": { "available": 3 } } }
  ```

| Status | Meaning |
|---|---|
| 401 | Missing or invalid token |
| 404 | Not found, including other users' resources (existence is never revealed) |
| 409 | Business conflict: stock, price change, used quote … |
| 410 | Action no longer pending |
| 413 | Body > 64 KB |
| 422 | Validation failed |
| 429 | Rate limited (`details.retry_after_seconds`) |

## Health and config

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/health/ready` | Database check, product count, active LLM provider (503 if the DB is down) |
| GET | `/config/public` | Ranking weight defaults, quantity limit, shipping rules, provider name |

## Auth

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/auth/register` | `{email, password, display_name}` | Password: 8–128 chars, letters + digits. Rate-limited per IP. |
| POST | `/auth/login` | `{email, password}` | Same response for an unknown email and a wrong password. Rate-limited. |
| GET 🔒 | `/auth/me` | — | Current user |

## Catalogue (public)

| Method | Path | Notes |
|---|---|---|
| GET | `/products` | `q`, `category`, `brand` (repeatable), `min_price`, `max_price`, `min_rating`, `in_stock`, `sort` (`relevance` · `price_asc` · `price_desc` · `rating` · `newest` · `name`), `page`, `page_size` (≤ 48) |
| GET | `/products/{id}` | Includes description and the full spec list |
| GET | `/products/compare?ids=1,2,3` | 2–4 ids; rows mark the `best` column index |
| GET | `/categories` | Counts, price ranges and spec schema per category |
| GET | `/brands?category=` | Distinct brands |

## Cart 🔒

| Method | Path | Body |
|---|---|---|
| GET | `/cart` | — |
| POST | `/cart/items` | `{product_id, quantity}` (strict integers, 1–100; the server enforces `MAX_ITEM_QUANTITY` and stock) |
| PATCH | `/cart/items/{product_id}` | `{quantity}` |
| DELETE | `/cart/items/{product_id}` | — |
| DELETE | `/cart` | — |

Every cart response carries server-computed totals: `subtotal_inr`, `shipping_inr`, `total_inr`, plus per-line `problem` flags (out of stock or above stock).

## Agent 🔒

| Method | Path | Notes |
|---|---|---|
| POST | `/chat` | `{message (≤ 2000), conversation_id?, weights?}`. Returns the user and assistant messages and any pending `actions`. |
| GET | `/conversations` | History (titles, counts) |
| GET | `/conversations/{id}` | Messages with structured payloads and their actions |
| DELETE | `/conversations/{id}` | — |
| GET | `/actions?status=pending` | The caller's proposed actions |
| POST | `/actions/{id}/approve` | Executes after revalidation. 409 `price_changed` returns `new_action_id`; 409 for stock problems; 410 if not pending or expired. Concurrent or repeated approve/reject requests for one action are serialised by a row lock: the first decides, the rest get 410 and change nothing. For a task's proposal, a price now outside the task's budget (`budget_max_inr` / `budget_min_inr` recorded in the payload) fails the action with 409 `outside_budget`, without adding it to the cart or creating a new proposal. |
| POST | `/actions/{id}/reject` | — |

The assistant message `payload` contains:

| Field | Contents |
|---|---|
| `type` | `recommendations`, `no_results`, `clarification`, `comparison`, `cart_proposal`, `preference_proposal`, `help`, `reset` or `error` |
| `understood` | Human-readable constraint chips |
| `recommendations[]` / `alternatives[]` | `score`, `breakdown`, `reasons`, `tradeoffs`, `unmet`, `violations`, `key_specs` |
| `comparison` | Comparison table data |
| `questions[]` | Clarifying questions |
| `notices[]` | Out-of-stock, limit and privacy notes |
| `applied_preferences[]` | Saved preferences used for this answer |
| `security_flags[]` | Injection patterns detected |
| `trace[]` | Workflow nodes with timings |
| `tool_calls[]` | Catalogue tool calls |
| `provider`, `llm_used` | Which provider ran, and whether an LLM contributed |
| `weights` | Ranking weights used |

## Preferences 🔒

| Method | Path | Body |
|---|---|---|
| GET | `/preferences` | — |
| POST | `/preferences` | `{kind, value, category?}` |
| PATCH | `/preferences/{id}` | `{value?, category?}` |
| DELETE | `/preferences/{id}` | — |

Rules:
* `kind` is one of `preferred_brand`, `avoided_brand`, `budget_max` (requires a category), `use_case` or `note`.
* Values that look like emails, phone or card numbers, identity numbers, passwords, addresses, or health and financial details are rejected with 422.

## Agent tasks 🔒 (persistent, read-only)

A task is multi-step agent work that runs in the background and survives restarts. The server plans the steps; clients choose only the task `kind` and its typed `input`. Every step calls a **read-only** tool: tasks never change the cart, place orders or take payment.

| Method | Path | Body / notes |
|---|---|---|
| POST | `/tasks` | `{kind: "product_research" | "shopping_agent", input: {...}, idempotency_key?}`. 201 with the queued task, which then runs in the background. Re-sending the same `idempotency_key` returns the original task (200); reusing it for different input is 409. 429 when the caller already has `TASK_MAX_ACTIVE_PER_USER` queued or running tasks. Rate-limited. |
| GET | `/tasks` | The caller's tasks, newest first (up to 50), without steps |
| GET | `/tasks/{id}` | One task with its steps |
| POST | `/tasks/{id}/cancel` | A queued or interrupted task is cancelled at once; a running one stops at the next step boundary (`cancel_requested: true`). 409 if already finished. |
| POST | `/tasks/{id}/resume` | Re-runs only the unfinished steps of a *stalled* queued task, an interrupted task, or a retryably failed task; finished steps are never repeated. 409 otherwise, including for a freshly queued task (it is about to run). |

`product_research` input (strict: unknown fields and type coercion are rejected):

| Field | Type | Default |
|---|---|---|
| `query` | string, 1–120 | — |
| `category` | one of the catalogue categories | — (a `query` or a `category` is required) |
| `max_price_inr` | integer ≥ 1 | — |
| `min_rating` | number 0–5 | — |
| `in_stock` | boolean | `true` |
| `compare_top` | integer 2–4 | `3` |
| `include_cart` | boolean | `true` |

A budget written in `query` ("laptop under ₹60,000", "below 60k", "between 40,000 and 60,000") is read by the same deterministic parser as chat: it becomes a price limit that can only lower `max_price_inr` (a typed minimum such as "above ₹60,000" is enforced too, and one above `max_price_inr` is rejected with 422 instead of being dropped), and it is removed from the search keywords (every keyword must match a product). The stored `input` keeps the text as typed.

Steps: `search` (`search_catalog`) → `compare` (`compare_products`; skipped when fewer than 2 products match) → `cart` (`read_cart`, optional) → `summarize` (built in, deterministic). The result has `matches`, `top`, `best` (lowest price / best rated), `cart` and `all_within_budget`.

`shopping_agent` input (strict). The server plans the steps one decision at a time:

| Field | Type | Default |
|---|---|---|
| `goal` | string, 3–500 (e.g. "a wireless mouse under ₹3,000") | required |
| `budget_max_inr` | integer ≥ 1; can only lower the budget found in `goal` | — |
| `category` | one of the catalogue categories; replaces the category found in `goal` | — |
| `allow_proposals` | boolean; lets the task *propose* adding a product to the cart (pending until you approve it) | `false` |

How it runs: the goal's category, budget and hard requirements are parsed deterministically. Then a bounded loop asks the planner (offline, rule-based by default; no API key) for one decision at a time: call a read-only tool, give a final answer, or ask a clarifying question. Every decision is checked **before** it runs:
* the tool must be one offered to the task (read-only tools only);
* the arguments must match the tool's schema;
* a final answer may only name products that `evaluate_constraints` passed.

A rejected decision is kept as a `skipped` step with an `error` and is never executed. An accepted tool call is saved as a step before it runs, so a resumed task finishes that step instead of planning again.

Limits:
* `TASK_MAX_ITERATIONS`: planner decisions. Exceeding it fails with `limit_reached`.
* `TASK_MAX_TOOL_CALLS`: executed tool calls. Exceeding it fails with `limit_reached`.
* `TASK_MAX_INVALID_DECISIONS`: rejected decisions allowed. Going over it fails with `invalid_decisions`.
* `TASK_DEADLINE_SECONDS`: time per run. Exceeding it fails with `deadline_exceeded`, which is retryable: resuming gets a fresh time budget.

A planner exception fails the task with `planner_error`, and no details are exposed.

The last step is the built-in `final` or `clarify`. The result has:
* `type`: `recommendations`, `no_match` or `clarification`;
* `products`: catalogue facts for eligible products only;
* `question`, `notes`, `requirements`, `candidates`, `eligible`, and `cart`.

**Search bounds and quantity (every planner).**
* `search_catalog` arguments are held to the task's category, budget and in-stock rule before a decision is checked or run. The step's `decision.clamped` lists the keys that were changed.
* A proposal's quantity must equal the quantity read from the goal; any other value is refused with `invented_quantity`.
* **Quantities belong to the product they name.** When the task may propose, the goal is checked before any research. A clarifying question is asked (the task ends as a `clarification`, with no tool calls) when:
  * quantities are given for more than one product ("add 2 mice and 3 keyboards");
  * a quantity belongs to another product ("… add 10 of the laptop" on a mouse task);
  * the quantity is above `MAX_ITEM_QUANTITY`.

  A quantity is never moved to another product or capped silently.
* **Budgets are per item.** "under ₹3,000" limits each item's price, not the cart total. Proposal summaries always show the per-item price, and the total for more than one unit, e.g. "Add 2 × X (₹1,195 each, ₹2,390 total) to your cart". The approval card shows "Your budget: up to ₹3,000 per item".

**Optional AI planner (`TASK_PLANNER=llm`, off by default).** It takes effect only when `LLM_PROVIDER` is a real model; with `mock`, tasks stay rule-based.
* **What the model can do:** choose the next step only, from the offered tools.
* **What's deterministic:** the category, budget, specs, stock, quantity and clarifying questions; the final notes are written by code; the model's rationale is replaced by a fixed label.
* **What the model sees:** the goal is sanitised and passed as data; observations are structured fields only.
* **How its output is checked:** strictly parsed, then validated exactly like any other decision.
* **Fallback:** a provider error, timeout, exhausted run time, or invalid or refused output switches the rest of that run to the rule-based planner.
* **Each step records:** `decision.source` (`llm` or `rule_based`), `decision.fallback` (the reason) and `decision.provider`.

**Cart proposals (approval-gated).**
* With `allow_proposals: true` (and `TASK_MAX_PROPOSALS` > 0), the planner may also call `propose_add_to_cart`. Its arguments are `product_id` and `quantity` only. The owner, price, task and step come from the server.
* Before creating anything, the tool re-checks the product against the task's own requirements and checks the quantity against `MAX_ITEM_QUANTITY` and stock.
* It creates one **pending** `add_to_cart` action, at most `TASK_MAX_PROPOSALS` (default 1) per task. The cart is not changed.
* The task still ends `succeeded` and lists the proposal in `result.proposals`. Its live status is in the task's `proposals`.
* Approve or reject it with `POST /actions/{id}/approve` or `/reject`. Approval re-checks the price and stock:
  * A price change creates a new proposal for the same task, but only if the new price is still within the task's budget.
  * A price outside the budget fails the action with 409 `outside_budget`, and no replacement is created.
* An action past its `expires_at` is reported as `expired` everywhere (including `GET /actions?status=…`) and can't be approved or rejected (410).
* A new chat proposal supersedes only actions that are still `pending` when it commits. An action being approved at that moment keeps its result.
* A proposal is tied to the step that made it (`agent_actions.task_step_id` is unique). If that step runs again after a resume or retry, it returns the same proposal (`reused: true`) instead of creating another.
* `product_research` tasks can never propose. There is no checkout tool and no permission that changes the cart.

Run again is offered only for `product_research` tasks.

Task fields: `status` (`queued` · `running` · `succeeded` · `failed` · `cancelled`), `progress` (0–100), `current_step`, `total_steps`, `result`, `error` (`{code, message, retryable, step}`), `cancel_requested`, `attempt` (runs started), `interrupted` (running but its runner's lease expired), `stalled` (queued, but nothing has happened for longer than `TASK_LEASE_SECONDS`, so its scheduled run was lost), `resumable`, and timestamps. Step fields: `status` (`queued` · `running` · `succeeded` · `failed` · `skipped` · `cancelled`), `attempts`, `max_attempts`, `args` (frozen when the step first runs), `output`, `error`. `shopping_agent` additions: the task's `state` (`planner`, `iterations`, `tool_calls`, `invalid_decisions`, `runs`, `deadline_at`) and each step's `decision` (`planner`, `iteration`, `kind`, `rationale`, `rejected`). Both are `null` for `product_research`. Every task also has `proposals`: its cart proposals as actions, with live `status`; empty for tasks that proposed nothing. Actions carry `task_id` when a task proposed them.

Controls shown on the Tasks page (the server re-checks every action):

| State | Cancel | Resume | Run again |
|---|---|---|---|
| queued | ✓ | only if `stalled` | — |
| running | ✓ (stops at the next step) | — | — |
| interrupted | ✓ | ✓ | — |
| succeeded | — | — | ✓ |
| failed | — | only if `retryable` | ✓ |
| cancelled | — | — | ✓ |

**Run again** creates a new task with `POST /tasks`, sending the original task's `input` (empty fields dropped) and a fresh `idempotency_key` that is reused if the request is retried, so a double click or a retry after a network error can't create a duplicate. The original task is not modified.

Recovery: transient tool failures (`temporarily_unavailable`) are retried with exponential backoff up to `TASK_STEP_MAX_ATTEMPTS`; if they persist the task fails with `retryable: true` and can be resumed. Permanent failures (`invalid_input`, `not_found`, `unknown_tool`, …) are not retryable. Other users' tasks are 404.

## Checkout 🔒 (demo; no payment)

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/checkout/quote` | — | Snapshot of the cart at live prices, valid for `CHECKOUT_QUOTE_TTL_SECONDS`. 409 if items have problems. |
| POST | `/checkout/confirm` | `{quote_id, confirm: true, idempotency_key}` | Locks product rows. 409 (with a `reason`) if the cart changed or the price or stock changed since the quote. Same key → same order (200). New order → 201. |
| GET | `/orders`, `/orders/{id}` | — | Own orders only |
