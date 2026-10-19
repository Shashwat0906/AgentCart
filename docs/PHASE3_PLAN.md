# Phase 3: AI planning loop with typed tool calling

Status: **in progress**, one PR per stage (D3). S1 and S2 merged in PR #14 (`ebda386`); S3 is on branch `phase3/s3-planning-loop`.

## Decisions (owner, 20 Oct 2026)

| # | Decision |
|---|---|
| D1 | **Migration `0003` is approved.** It must be backward compatible (additive and nullable), with appropriate indexes and constraints, and tested on both SQLite and PostgreSQL. Migrations are never run against production during development; production applies them only through the normal deploy (`alembic upgrade head` at container start) after the PR is reviewed and merged. |
| D2 | **Approval workflow (Phase 3 approach).** A task that proposes a protected action finishes its research and planning as `succeeded`, with the pending proposals listed in its result. Approval is a separate, explicit, authenticated operation (the existing `POST /api/actions/{id}/approve`, which revalidates price and stock). Protected actions are never executed automatically. Proposals are persisted, and approval is idempotent. A dedicated `awaiting_approval` task state can be added in Phase 4 if genuinely needed. |
| D3 | **One PR per stage.** Stages S1–S6 are separate commits on `phase3/ai-planning-loop`. The relevant tests run after every stage, and the complete regression suite runs before the final PR. Nothing is merged into `main` or deployed until the owner has reviewed the PR. |

## Invariants (every stage)

* The mock or offline provider works without any API key. Production stays on `LLM_PROVIDER=mock` unless the owner changes it.
* The existing task lifecycle (`queued` → `running` → `succeeded` / `failed` / `cancelled`, leases, retries, cancel, resume, Run again) and all existing APIs keep working. Changes to them are additive only.
* The model may choose only from explicitly registered, authorized tools. Plans and arguments are schema-validated. No model-generated code or SQL is ever executed.
* Hard constraints and the budget come from the user's input and the deterministic parser, never from a model. Prices, stock, eligibility and ranking come from application data.
* There is no `execute` permission and no checkout tool. Cart changes happen only through user approval, with revalidation.
* The trace records decisions, tool calls, results and failures, never credentials, secrets or raw model prompts.

## Architecture

The persistent `TaskRunner` gains a second mode for a new task kind, `shopping_agent`. The chat agent (LangGraph) is unchanged.

```
goal ─► RequestParser + validate_intent (deterministic intent and hard constraints)
     ─► loop, bounded by iterations, tool calls, invalid decisions and a task deadline:
          decided-but-unfinished step? run it (resume never re-plans)
          decision = Planner.decide(state, allowed tools)      ← typed PlannerDecision
          persist the decision as a step (frozen args) → commit, then execute via ToolRegistry
     ─► final answer built only from tool outputs
```

* **Planner interface** (`app/agents/planner.py`): `decide(state, tools) -> ToolCall | Final | Clarify`, strict schemas.
  * `RuleBasedPlanner` is the default: deterministic and offline.
  * `LLMPlanner` (S5) is optional: it uses `LLMProvider.complete_json`, its output is validated, and anything invalid falls back to the rule-based planner.
* **Tools:**
  * reused: `search_catalog`, `get_product`, `compare_products`, `read_cart`;
  * new read tool `evaluate_constraints` (S2): the hard constraints come from the task, not from tool arguments;
  * new `propose` tool `propose_add_to_cart` (S4): creates a pending `agent_action` only.
* **Migration `0003`** (S3/S4):
  * `agent_tasks.state` (JSON): loop counters and the deadline;
  * `agent_task_steps.decision` (JSON): planner metadata;
  * `agent_actions.task_step_id` (nullable FK, unique, indexed): duplicate-proof proposals.

## Stages and acceptance criteria

| Stage | Scope | Accepted when |
|---|---|---|
| **S1** | Planner contracts and `RuleBasedPlanner`, not wired into the runner | Decision schemas reject malformed, unknown and oversized input. The rule-based plans are correct for: matches exist; no matches; fewer than two matches; "add" requested with and without permission; missing category (clarify); a missing tool; failed observations. Decisions are deterministic, there is no network or provider dependency, and the final-answer grounding check works. All existing tests pass unchanged. |
| **S2** | `evaluate_constraints` read tool | Agrees with `filter_products` across the catalogue; budget and RAM enforced; strict input; read-only guard; existing registry tests pass. |
| **S3** | Migration `0003` and the loop runner (read tools only) | Multi-step end to end; every bound enforced; invalid or unauthorized decisions rejected before execution; retries, cancel, interrupted resume without re-planning finished steps; idempotent create; Phase 2 and 2.1 tests unchanged; migration upgrade (with data), downgrade and `alembic check` on SQLite and PostgreSQL. |
| **S4** | Approval-gated proposals | Pending actions only, with the cart unchanged; proposals refused where not permitted or where they would break hard constraints; no duplicates on resume; approval revalidates price and stock and is idempotent; no checkout tool. |
| **S5** | Optional `LLMPlanner` | Works with no key; unknown or unauthorized tools, prompt injection, malformed output, timeouts and invented budgets can't widen capability or constraints. |
| **S6** | API, frontend, E2E, docs, evaluation | Full regression on SQLite and PostgreSQL, strict evaluation, Playwright, CI green; production configuration unchanged. |

## Progress

| Stage | Status | Commit / PR |
|---|---|---|
| S1 | ✅ Planner contracts and `RuleBasedPlanner` (`app/agents/planner.py`); 51 tests | `f96704f`, PR #14 |
| S2 | ✅ `evaluate_constraints` read tool (registry), requirements taken from `ToolContext.constraints`; 40 tests | PR #14 |
| S3 | ✅ Migration `0003` and the planning loop for `shopping_agent` tasks (`TaskRunner._drive_loop`), read tools only; 33 tests | PR (this branch) |
| S4–S6 | Not started | — |

### Notes from S2

* `evaluate_constraints` reuses `ranking.evaluate` (the same check that filters recommendations) and the existing violation wording via the new public `ranking.violation_message`. A test asserts it agrees with `filter_products` across the whole catalogue for nine goals.
* The tool's arguments are product ids only. Category, hard constraints (budget included) and stock come from the task. An unknown attribute counts as a failure.
* Known cosmetic limitation (existing wording, unchanged): an attribute the product's category doesn't define is shown by its raw name, e.g. `ram gb unknown (you required RAM ≥ 16 GB)` for a mouse.

### Notes from S3

* **Migration `0003`** contains only the S3 columns: `agent_tasks.state` and `agent_task_steps.decision`, both nullable JSON and with no index (never filtered on). `agent_actions.task_step_id` moves to S4 (it would be `0004`), so that S3 ships no unused column.
* **Loop:**
  * Each iteration: lease check → cancel check → finish any decided-but-unfinished step → limits → `planner.decide` → `check_decision` plus argument validation against the tool's own input model → persist the step (commit) → execute through the existing `_run_step_with_retries`.
  * Hard constraints are passed to tools via `ToolContext.constraints`, built from the deterministic intent.
* **Bounds:** iterations, tool calls, invalid decisions, and a per-run deadline (`TASK_*` settings). The deadline is the only retryable limit. Counters are totals across runs.
* **Trace:**
  * Rejected decisions are recorded as `skipped` steps with `decision.rejected = true` and are never executed or shown to the planner as observations.
  * The final step is a built-in `final` or `clarify` step whose output is the grounded result.
* **Frontend:** a compatibility guard only. `AgentTask` is a union on `kind`. The research summary and Run again appear only for `product_research`. The agent UI is S6.
* **Mutation checks:** removing argument validation, `check_decision`, resume-without-replan, each limit, the in-loop cancel check, or the constraints hand-off makes at least one test fail. Two mutants survive, for reasons that make them harmless:
  * the permission filter, which the registry already enforces;
  * the commit after persisting a step, which `_run_step_with_retries` immediately repeats.
