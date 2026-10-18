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
| POST | `/actions/{id}/approve` | Executes after revalidation. 409 `price_changed` returns `new_action_id`; 409 for stock problems; 410 if not pending or expired. |
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

## Checkout 🔒 (demo; no payment)

| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/checkout/quote` | — | Snapshot of the cart at live prices, valid for `CHECKOUT_QUOTE_TTL_SECONDS`. 409 if items have problems. |
| POST | `/checkout/confirm` | `{quote_id, confirm: true, idempotency_key}` | Locks product rows. 409 (with a `reason`) if the cart changed or the price or stock changed since the quote. Same key → same order (200). New order → 201. |
| GET | `/orders`, `/orders/{id}` | — | Own orders only |
