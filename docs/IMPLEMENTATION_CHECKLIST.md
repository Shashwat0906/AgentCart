# AgentCart — Implementation Checklist

Status legend: `[ ]` todo · `[x]` done and verified

## Phase 0 — Workspace inspection
- [x] Workspace `/Users/shashwatroy/Desktop/AgentCart` confirmed empty, not a git repo
- [x] Toolchain: Python 3.14, Node 22, npm 10, Docker 28 (daemon must be started), Git 2.39
- [x] Host already runs a password-protected PostgreSQL on 5432 → project Postgres uses **5433**
- [x] `git init -b main`; global identity left untouched

## Phase 1 — Backend foundation
- [ ] FastAPI app factory, settings (pydantic-settings), structured errors, health checks
- [ ] SQLAlchemy 2.0 models: users, products, carts, cart_items, conversations, messages, agent_actions, preferences, orders, order_items
- [ ] Alembic migration (initial schema)
- [ ] Seed script: ≥100 products across ≥8 categories with INR prices, specs, stock
- [ ] Catalog API: keyword search, filters, sorting, pagination, product detail, comparison

## Phase 2 — Ranking + agent
- [ ] Generic constraint model (attribute / operator / value / hard|soft)
- [ ] Deterministic ranking engine with configurable weights + explanations + trade-offs
- [ ] Rule-based (mock) request parser — default, no API key
- [ ] Provider interface: mock, Ollama, OpenAI-compatible hosted, Anthropic
- [ ] LangGraph workflow: parse → extract → validate → (clarify | search) → rank → explain → propose → await approval
- [ ] Bounded retries, timeouts, safe fallbacks, workflow trace
- [ ] Grounding check: LLM text may not mention products/prices outside the result set
- [ ] Prompt-injection handling (user messages + product descriptions treated as data)

## Phase 3 — Auth, memory, cart, checkout
- [ ] Register / login, Argon2 password hashing, JWT access tokens
- [ ] Ownership checks on conversations, preferences, carts, orders, agent actions
- [ ] Preference memory CRUD + sensitive-data guard + approval-gated agent proposals
- [ ] Cart CRUD with backend totals, quantity + stock validation
- [ ] Agent action approval / rejection with price + stock revalidation
- [ ] Checkout quote → confirm (row locks, idempotency key, price-change detection, no real payment)
- [ ] CORS from env, rate limiting, safe error handling

## Phase 4 — Frontend
- [ ] Vite + React + TS + Tailwind + React Router + TanStack Query
- [ ] Home, catalog (search / filters / sort / pagination), product detail, compare
- [ ] AI chat with recommendation cards, comparison table, trace, approval controls
- [ ] Cart, checkout (quote → confirm), orders
- [ ] Preferences, conversation history, auth pages
- [ ] Loading / empty / error states; responsive; accessible

## Phase 5 — Tests + evaluation
- [ ] Backend unit + integration tests (pytest)
- [ ] ≥50 deterministic evaluation scenarios + metrics report
- [ ] Frontend unit tests (Vitest + Testing Library)
- [ ] End-to-end tests (Playwright) against the running stack

## Phase 6 — Infra
- [ ] Dockerfiles, docker-compose (db, backend, frontend, optional ollama profile)
- [ ] GitHub Actions: lint, type-check, tests, eval, builds, secret scan
- [ ] Deployment config (Render blueprint, Vercel, Neon) + docs

## Phase 7 — Verification + docs
- [ ] Run all suites, lint, type checks, builds; fix failures
- [ ] Full stack verified locally via Docker Compose
- [ ] README with exact commands + actual results
