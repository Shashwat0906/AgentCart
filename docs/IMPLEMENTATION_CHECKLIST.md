# AgentCart — Implementation Checklist

Status legend: `[ ]` todo · `[x]` done and verified

## Phase 0 — Workspace inspection
- [x] Workspace `/Users/shashwatroy/Desktop/AgentCart` confirmed empty, not a git repo
- [x] Toolchain: Python 3.14, Node 22, npm 10, Docker 28 (daemon must be started), Git 2.39
- [x] Host already runs a password-protected PostgreSQL on 5432 → project Postgres uses **5433**
- [x] `git init -b main`; global identity left untouched

## Phase 1 — Backend foundation
- [x] FastAPI app factory, settings (pydantic-settings), structured errors, health checks
- [x] SQLAlchemy 2.0 models: users, products, carts, cart_items, conversations, messages, agent_actions, preferences, orders, order_items
- [x] Alembic migration (initial schema)
- [x] Seed script: ≥100 products across ≥8 categories with INR prices, specs, stock
- [x] Catalog API: keyword search, filters, sorting, pagination, product detail, comparison

## Phase 2 — Ranking + agent
- [x] Generic constraint model (attribute / operator / value / hard|soft)
- [x] Deterministic ranking engine with configurable weights + explanations + trade-offs
- [x] Rule-based (mock) request parser — default, no API key
- [x] Provider interface: mock, Ollama, OpenAI-compatible hosted, Anthropic
- [x] LangGraph workflow: parse → extract → validate → (clarify | search) → rank → explain → propose → await approval
- [x] Bounded retries, timeouts, safe fallbacks, workflow trace
- [x] Grounding check: LLM text may not mention products/prices outside the result set
- [x] Prompt-injection handling (user messages + product descriptions treated as data)

## Phase 3 — Auth, memory, cart, checkout
- [x] Register / login, Argon2 password hashing, JWT access tokens
- [x] Ownership checks on conversations, preferences, carts, orders, agent actions
- [x] Preference memory CRUD + sensitive-data guard + approval-gated agent proposals
- [x] Cart CRUD with backend totals, quantity + stock validation
- [x] Agent action approval / rejection with price + stock revalidation
- [x] Checkout quote → confirm (row locks, idempotency key, price-change detection, no real payment)
- [x] CORS from env, rate limiting, safe error handling

## Phase 4 — Frontend
- [x] Vite + React + TS + Tailwind + React Router + TanStack Query
- [x] Home, catalog (search / filters / sort / pagination), product detail, compare
- [x] AI chat with recommendation cards, comparison table, trace, approval controls
- [x] Cart, checkout (quote → confirm), orders
- [x] Preferences, conversation history, auth pages
- [x] Loading / empty / error states; responsive; accessible

## Phase 5 — Tests + evaluation
- [x] Backend unit + integration tests (pytest)
- [x] ≥50 deterministic evaluation scenarios + metrics report
- [x] Frontend unit tests (Vitest + Testing Library)
- [x] End-to-end tests (Playwright) against the running stack

## Phase 6 — Infra
- [x] Dockerfiles, docker-compose (db, backend, frontend, optional ollama profile)
- [x] GitHub Actions: lint, type-check, tests, eval, builds, secret scan
- [x] Deployment config (Render blueprint, Vercel, Neon) + docs

## Phase 7 — Verification + docs
- [x] Run all suites, lint, type checks, builds; fix failures
- [x] Full stack verified locally via Docker Compose
- [x] README with exact commands + actual results

## Agentic commerce upgrade
- [x] Phase 1: typed tool registry for read-only tools (search, product, compare, cart)
- [x] Phase 2: persistent agent tasks (migration `0002`, leased runner, retries, cancel, resume, Tasks page)
- [ ] Phase 3: planning loop with LLM tool calling
- [ ] Phase 4: approval-gated cart management actions
- [ ] Phase 5: approval-gated checkout
- [ ] Phase 6: broader error recovery (substitutes, price-change re-proposals)

## Not done (needs owner approval or external resources)
- [ ] Push to GitHub and observe the first CI run
- [ ] Deploy to Vercel / Render / Neon (configs prepared, nothing created)
- [ ] Exercise the Ollama / hosted-LLM providers against a real model

## Bugs found and fixed during verification
- Duplicate category-less preferences slipped past the NULL-aware unique constraint (backend tests)
- "4K monitor" parsed as a ₹4,000 budget (evaluation suite)
- "always …" in an injected message triggered a save-preference proposal (evaluation suite)
- Session lost on page reload: API client configured after child effects ran (browser walkthrough)
- Raw bidi / zero-width characters in source files replaced by escapes (lint review)
- Clarifying question rendered twice; product image stretched; chat sidebar not sticky (visual review)
- Compare-by-name picked the wrong variant ("Galaxy S24" → S24 Ultra; full names truncated by the 8-token search cap) (pre-publish black-box verification)
- "Add <exact product name> to my cart" asked a clarifying question, and "add iPhone 15 Pro" proposed the base iPhone 15 (pre-publish black-box verification)
- "The cheapest X" only up-weighted price, so the top pick and the cart proposal were not always the lowest-priced match (pre-publish black-box verification)
