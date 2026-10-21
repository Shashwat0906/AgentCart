"""Phase 3 S3: the bounded planning loop for `shopping_agent` tasks, run by the persistent TaskRunner.

Uses the offline `RuleBasedPlanner` (no provider, no API key) and scripted planners for failure cases.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.agents.planner import Clarify, Final, PlannerState, RuleBasedPlanner, ToolCall
from app.agents.tool_registry import ToolContext
from app.core.config import get_settings
from app.models import AgentAction, AgentTask, Cart, CartItem, Order, Preference, Product
from app.services import tasks as task_service
from app.services.tasks import TaskRunner
from tests.conftest import register
from tests.test_tasks import Calls, _Crash, counts, fetch, statuses, task_row, transient, uid

MOUSE = {"goal": "a wireless mouse under ₹3,000"}
NO_MATCH = {"goal": "laptops strictly under ₹50,000 with 16GB RAM"}
Decision = ToolCall | Final | Clarify


# ------------------------------------------------------------------ helpers
class Scripted:
    """A planner that replays decisions in order (then repeats the last one) and counts its calls."""

    name = "scripted"

    def __init__(self, *decisions: Decision | Callable[[PlannerState], Decision]) -> None:
        self.decisions = list(decisions)
        self.calls = 0
        self.seen: list[PlannerState] = []

    def decide(self, state: PlannerState, tools: Sequence[Mapping[str, Any]]) -> Decision:
        self.seen.append(state)
        d = self.decisions[min(self.calls, len(self.decisions) - 1)]
        self.calls += 1
        return d(state) if callable(d) else d


class CountingRuleBased(RuleBasedPlanner):
    calls = 0

    def decide(self, state: PlannerState, tools: Sequence[Mapping[str, Any]]) -> Decision:
        type(self).calls += 1
        return super().decide(state, tools)


def agent_task(SessionLocal: sessionmaker[Session], user_id: int, inp: dict[str, Any] | None = None) -> str:
    with SessionLocal() as s:
        task, _ = task_service.create_task(
            s, user_id, "shopping_agent", MOUSE if inp is None else inp, None, get_settings()
        )
        return task.id


def loop_runner(
    SessionLocal: sessionmaker[Session],
    planner: Any = None,
    calls: Calls | None = None,
    clock: Callable[[], datetime] | None = None,
    **settings: Any,
) -> TaskRunner:
    s = get_settings().model_copy(update=settings) if settings else get_settings()
    kwargs: dict[str, Any] = {"sleep": lambda _x: None}
    if planner is not None:
        kwargs["planner_factory"] = lambda: planner
    if calls is not None:
        kwargs["registry_factory"] = calls.registry
    if clock is not None:
        kwargs["clock"] = clock
    return TaskRunner(SessionLocal, s, **kwargs)


def start_agent(client: TestClient, h: dict[str, str], inp: dict[str, Any] | None = None, **extra: Any) -> Any:
    return client.post(
        "/api/tasks", json={"kind": "shopping_agent", "input": MOUSE if inp is None else inp, **extra}, headers=h
    )


def mouse_ids(client: TestClient) -> list[int]:
    return [
        p["id"] for p in client.get("/api/products", params={"category": "mice", "in_stock": "true"}).json()["items"]
    ]


SEARCH_MICE = ToolCall(tool="search_catalog", args={"category": "mice", "in_stock": True, "page_size": 24})


def evaluate_found(state: PlannerState) -> Decision:
    obs = state.latest("search_catalog")
    assert obs is not None and obs.data is not None
    return ToolCall(tool="evaluate_constraints", args={"product_ids": [int(p["id"]) for p in obs.data["items"]]})


def final_eligible(state: PlannerState) -> Decision:
    return Final(product_ids=(state.eligible_ids() or [])[:2], notes="done")


def resume(SessionLocal: sessionmaker[Session], client: TestClient, h: dict[str, str], tid: str) -> None:
    """The API's resume preparation, without scheduling a run (the test runs its own instrumented runner)."""
    with SessionLocal() as s:
        task_service.prepare_resume(s, uid(client, h), tid, get_settings())


# ------------------------------------------------------------------ end to end with the offline planner
def test_agent_task_plans_executes_and_answers_from_tool_outputs(client: TestClient) -> None:
    h = register(client)
    r = start_agent(client, h)
    assert r.status_code == 201 and r.json()["kind"] == "shopping_agent" and r.json()["status"] == "queued"
    assert r.json()["goal"] == "Agent: a wireless mouse under ₹3,000"
    t = fetch(client, h, r.json()["id"])
    assert t["status"] == "succeeded" and t["progress"] == 100 and t["error"] is None
    assert [s["tool"] for s in t["steps"]] == [
        "search_catalog",
        "evaluate_constraints",
        "compare_products",
        "read_cart",
        "final",
    ]
    assert all(s["status"] == "succeeded" for s in t["steps"])
    assert t["current_step"] == t["total_steps"] == 5
    # every step records the decision that produced it
    assert [s["decision"]["iteration"] for s in t["steps"]] == [1, 2, 3, 4, 5]
    assert all(s["decision"]["planner"] == "rule_based" and not s["decision"].get("rejected") for s in t["steps"])
    assert t["state"]["iterations"] == 5 and t["state"]["tool_calls"] == 4 and t["state"]["invalid_decisions"] == 0
    assert t["state"]["runs"] == 1 and t["state"]["planner"] == "rule_based" and t["state"]["deadline_at"]

    # the answer is grounded: every product passed the constraint check, and facts are the catalogue's
    res = t["result"]
    assert res["type"] == "recommendations" and 1 <= len(res["products"]) <= 3
    report = t["steps"][1]["output"]
    passed = {r["product_id"] for r in report["results"] if r["passes"]}
    assert {p["id"] for p in res["products"]} <= passed and res["eligible"] == len(passed)
    for p in res["products"]:
        truth = client.get(f"/api/products/{p['id']}").json()
        assert p["price_inr"] == truth["price_inr"] <= 3_000 and p["name"] == truth["name"]
        assert p["category"] == "mice" and p["in_stock"] is True
    assert res["cart"] == {"item_count": 0, "total_inr": 0}
    assert res["requirements"] == report["constraints"] and res["requirements"]


def test_agent_task_with_no_eligible_product_returns_no_match(client: TestClient) -> None:
    h = register(client)
    t = fetch(client, h, start_agent(client, h, NO_MATCH).json()["id"])
    assert t["status"] == "succeeded"
    res = t["result"]
    assert res["type"] == "no_match" and res["products"] == [] and res["eligible"] == 0
    assert res["candidates"] >= 1 and "meets every requirement" in res["notes"]
    assert [s["tool"] for s in t["steps"]] == ["search_catalog", "evaluate_constraints", "final"]


def test_agent_task_asks_a_question_without_calling_tools(client: TestClient) -> None:
    h = register(client)
    t = fetch(client, h, start_agent(client, h, {"goal": "something nice please"}).json()["id"])
    assert t["status"] == "succeeded"
    assert [s["tool"] for s in t["steps"]] == ["clarify"]
    assert t["result"]["type"] == "clarification" and t["result"]["question"]
    assert t["result"]["products"] == [] and t["state"]["tool_calls"] == 0


def test_agent_task_proposes_but_never_changes_the_cart(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """S4 replaces the S3 rule "never proposes": an opted-in task may create one *pending* proposal, and
    the cart still never changes without the user's approval (see test_agent_proposals.py)."""
    h = register(client)
    with SessionLocal() as s:
        before = counts(s, AgentAction, CartItem)
    t = fetch(
        client,
        h,
        start_agent(
            client, h, {"goal": "add a wireless mouse under ₹3,000 to my cart", "allow_proposals": True}
        ).json()["id"],
    )
    assert t["status"] == "succeeded" and "propose_add_to_cart" in {s["tool"] for s in t["steps"]}
    assert [p["status"] for p in t["proposals"]] == ["pending"]
    with SessionLocal() as s:
        after = counts(s, AgentAction, CartItem)
    assert after == {"agent_actions": before["agent_actions"] + 1, "cart_items": before["cart_items"]}


def test_structured_inputs_only_tighten_the_goal(client: TestClient) -> None:
    h = register(client)
    t = fetch(
        client,
        h,
        start_agent(client, h, {"goal": "a wireless mouse under ₹3,000", "budget_max_inr": 1_000}).json()["id"],
    )
    search_args = t["steps"][0]["args"]
    assert search_args["max_price_inr"] == 1_000
    assert all(p["price_inr"] <= 1_000 for p in t["result"]["products"])


def test_agent_input_is_validated(client: TestClient) -> None:
    h = register(client)
    for bad in (
        {},
        {"goal": "x"},
        {"goal": "mice", "extra": 1},
        {"goal": "mice", "category": "rockets"},
        {"goal": "mice", "budget_max_inr": 0},
    ):
        assert start_agent(client, h, bad).status_code == 422, bad
    r = client.post("/api/tasks", json={"kind": "free_form", "input": MOUSE}, headers=h)
    assert r.status_code == 422


# ------------------------------------------------------------------ bounds
def test_iteration_limit_stops_the_loop(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    planner = Scripted(SEARCH_MICE)  # never finishes
    assert loop_runner(SessionLocal, planner, task_max_iterations=3, task_max_tool_calls=50).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"] == {"code": "limit_reached", "message": "Stopped after 3 planning steps", "retryable": False}
    assert planner.calls == 3 and len(t["steps"]) == 3 and t["resumable"] is False
    assert task_row(SessionLocal, tid).locked_by is None


def test_tool_call_limit_stops_before_the_extra_call(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()
    assert loop_runner(SessionLocal, Scripted(SEARCH_MICE), calls, task_max_tool_calls=2).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"]["code"] == "limit_reached" and "2 tool calls" in t["error"]["message"]
    assert calls.count == Counter({"search_catalog": 2}) and len(t["steps"]) == 2  # the third was never persisted
    assert t["state"]["tool_calls"] == 2


def test_deadline_stops_the_run_and_a_resume_gets_a_fresh_budget(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    now = [datetime(2026, 10, 20, tzinfo=UTC)]

    def clock() -> datetime:
        return now[0]

    calls = Calls()
    calls.behaviour["search_catalog"] = lambda ctx, n: now.__setitem__(0, now[0] + timedelta(seconds=61))
    assert loop_runner(SessionLocal, None, calls, clock).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"] == {"code": "deadline_exceeded", "message": "Stopped after 60 seconds", "retryable": True}
    assert statuses(t) == [("search_catalog", "succeeded")] and t["resumable"] is True

    resume(SessionLocal, client, h, tid)
    calls.behaviour.clear()
    assert loop_runner(SessionLocal, None, calls, clock).run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert t["state"]["runs"] == 2
    assert calls.count["search_catalog"] == 1  # finished before the deadline: never repeated


def test_repeated_invalid_decisions_fail_the_task(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()
    planner = Scripted(ToolCall(tool="add_to_cart", args={"product_id": 1, "quantity": 1}))
    assert loop_runner(SessionLocal, planner, calls, task_max_invalid_decisions=2).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"]["code"] == "invalid_decisions" and t["error"]["retryable"] is False
    assert planner.calls == 3 and sum(calls.count.values()) == 0
    assert [(s["name"], s["status"], s["error"]["code"]) for s in t["steps"]] == [
        ("rejected:add_to_cart", "skipped", "unknown_tool")
    ] * 3
    assert all(s["decision"]["rejected"] is True and s["attempts"] == 0 for s in t["steps"])


# ------------------------------------------------------------------ validation before execution
@pytest.mark.parametrize(
    ("decision", "code"),
    [
        (ToolCall(tool="add_to_cart", args={"product_id": 1, "quantity": 1}), "unknown_tool"),
        (ToolCall(tool="checkout"), "unknown_tool"),
        (ToolCall(tool="propose_add_to_cart", args={"product_id": 1, "quantity": 1}), "unknown_tool"),
        (ToolCall(tool="search_catalog", args={"category": "mice", "sql": "DROP TABLE users"}), "invalid_arguments"),
        (ToolCall(tool="search_catalog", args={"page_size": 10_000}), "invalid_arguments"),
        (ToolCall(tool="evaluate_constraints", args={"product_ids": [1], "max_price_inr": 10**9}), "invalid_arguments"),
        (ToolCall(tool="get_product", args={"product_id": "1; DROP"}), "invalid_arguments"),
        (Final(product_ids=[1, 2]), "ungrounded_final"),
    ],
)
def test_invalid_decisions_are_rejected_before_anything_runs(
    client: TestClient, SessionLocal: sessionmaker[Session], decision: Decision, code: str
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()
    planner = Scripted(decision, Final(notes="gave up"))
    assert loop_runner(SessionLocal, planner, calls).run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert sum(calls.count.values()) == 0  # nothing executed
    rejected, final = t["steps"]
    assert rejected["status"] == "skipped" and rejected["error"]["code"] == code and rejected["attempts"] == 0
    assert rejected["decision"]["rejected"] is True
    assert final["tool"] == "final" and t["result"]["products"] == []
    assert t["state"]["invalid_decisions"] == 1 and t["state"]["tool_calls"] == 0
    # the planner never sees a rejected decision as an observation
    assert planner.seen[1].observations == ()


def test_unoffered_tools_are_rejected_even_if_registered(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    offered: list[set[str]] = []

    class Spy(Scripted):
        def decide(self, state: PlannerState, tools: Sequence[Mapping[str, Any]]) -> Decision:
            offered.append({str(t["name"]) for t in tools})
            return super().decide(state, tools)

    loop_runner(SessionLocal, Spy(Final())).run(tid)
    assert offered[0] == {"search_catalog", "get_product", "compare_products", "read_cart", "evaluate_constraints"}


def test_a_final_answer_must_be_grounded_in_a_constraint_check(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    expensive = max(
        client.get("/api/products", params={"category": "mice"}).json()["items"], key=lambda p: p["price_inr"]
    )["id"]
    planner = Scripted(
        SEARCH_MICE, evaluate_found, Final(product_ids=[expensive]), final_eligible
    )  # tries to recommend an over-budget mouse first
    assert loop_runner(SessionLocal, planner).run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert [s["status"] for s in t["steps"]] == ["succeeded", "succeeded", "skipped", "succeeded"]
    assert t["steps"][2]["error"]["code"] == "ungrounded_final"
    assert expensive not in {p["id"] for p in t["result"]["products"]}
    assert all(p["price_inr"] <= 3_000 for p in t["result"]["products"])


def test_planner_cannot_widen_the_budget_through_arguments(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """A planner can't widen the task through search arguments (S5: they are held to the task's category, budget
    and stock), and eligibility never depends on the search: it comes from the task's own constraints."""
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    wide = ToolCall(
        tool="search_catalog",
        args={"category": "laptops", "max_price_inr": 10_000_000, "in_stock": False, "page_size": 24},
    )
    planner = Scripted(wide, evaluate_found, final_eligible)
    assert loop_runner(SessionLocal, planner).run(tid) == "succeeded"
    t = fetch(client, h, tid)
    search = t["steps"][0]
    assert search["args"]["category"] == "mice" and search["args"]["max_price_inr"] == 3_000
    assert search["args"]["in_stock"] is True
    assert sorted(search["decision"]["clamped"]) == ["category", "in_stock", "max_price_inr"]
    assert search["output"]["items"] and all(
        p["category"] == "mice" and p["price_inr"] <= 3_000 and p["in_stock"] for p in search["output"]["items"]
    )
    assert all(p["price_inr"] <= 3_000 for p in t["result"]["products"])

    # eligibility is the task's: an over-budget mouse checked directly still fails
    tid2 = agent_task(SessionLocal, uid(client, h))
    expensive = max(
        client.get("/api/products", params={"category": "mice"}).json()["items"], key=lambda p: p["price_inr"]
    )
    check = ToolCall(tool="evaluate_constraints", args={"product_ids": [expensive["id"]]})
    assert loop_runner(SessionLocal, Scripted(check, Final())).run(tid2) == "succeeded"
    [result] = fetch(client, h, tid2)["steps"][0]["output"]["results"]
    assert result["passes"] is False and any("over your budget" in v for v in result["violations"])


def test_planner_exceptions_fail_the_task_without_leaking_details(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))

    def boom(state: PlannerState) -> Decision:
        raise RuntimeError("secret-provider-token-xyz")

    assert loop_runner(SessionLocal, Scripted(boom)).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"]["code"] == "planner_error" and "secret" not in str(t)
    assert task_row(SessionLocal, tid).locked_by is None


# ------------------------------------------------------------------ retries, failures, resume, cancel, leases
def test_transient_tool_failures_are_retried(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()
    calls.behaviour["search_catalog"] = transient(2)
    planner = CountingRuleBased()
    CountingRuleBased.calls = 0
    assert loop_runner(SessionLocal, planner, calls).run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert t["steps"][0]["attempts"] == 3 and calls.count["search_catalog"] == 3
    assert CountingRuleBased.calls == 5  # retries never ask the planner again


def test_permanent_tool_failure_fails_the_task(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()
    planner = Scripted(ToolCall(tool="get_product", args={"product_id": 999_999}))
    assert loop_runner(SessionLocal, planner, calls).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert (
        t["error"]["code"] == "not_found" and t["error"]["retryable"] is False and t["error"]["step"] == "get_product"
    )
    assert statuses(t) == [("get_product", "failed")] and t["steps"][0]["attempts"] == 1
    assert t["resumable"] is False


def test_retryable_failure_resumes_the_same_step_without_replanning(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()
    calls.behaviour["evaluate_constraints"] = transient(99)
    CountingRuleBased.calls = 0
    assert loop_runner(SessionLocal, CountingRuleBased(), calls).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"]["code"] == "temporarily_unavailable" and t["resumable"] is True
    assert statuses(t) == [("search_catalog", "succeeded"), ("evaluate_constraints", "failed")]
    frozen_args = t["steps"][1]["args"]
    assert CountingRuleBased.calls == 2

    calls.behaviour.clear()
    resume(SessionLocal, client, h, tid)
    assert loop_runner(SessionLocal, CountingRuleBased(), calls).run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert t["steps"][1]["args"] == frozen_args and t["steps"][1]["status"] == "succeeded"
    assert calls.count["search_catalog"] == 1  # never repeated
    # 2 decisions in run 1; run 2 executes the persisted step first and then plans 3 more (compare, cart, final)
    assert t["steps"][1]["attempts"] == 1  # attempts reset by resume; the step was not re-decided
    assert CountingRuleBased.calls == 5 and t["state"]["runs"] == 2 and t["state"]["iterations"] == 5
    assert [s["tool"] for s in t["steps"]].count("evaluate_constraints") == 1


def test_crash_mid_step_resumes_the_decided_step(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()

    def die(ctx: ToolContext, n: int) -> None:
        raise _Crash

    calls.behaviour["compare_products"] = die
    planner = CountingRuleBased()
    CountingRuleBased.calls = 0
    with pytest.raises(_Crash):
        loop_runner(SessionLocal, planner, calls).run(tid)
    t = task_row(SessionLocal, tid)
    assert t.status == "running" and statuses(t)[-1] == ("compare_products", "running")
    decided_args = t.steps[-1].args
    assert CountingRuleBased.calls == 3

    with SessionLocal() as s:  # the dead runner's lease expires
        row = s.get(AgentTask, tid)
        assert row is not None
        row.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    assert fetch(client, h, tid)["interrupted"] is True

    calls.behaviour.clear()
    assert loop_runner(SessionLocal, planner, calls).run(tid) == "succeeded"
    t = task_row(SessionLocal, tid)
    compare = next(st for st in t.steps if st.tool == "compare_products")
    assert compare.args == decided_args and compare.attempts == 2
    assert calls.count["search_catalog"] == 1 and calls.count["evaluate_constraints"] == 1
    assert [st.tool for st in t.steps].count("compare_products") == 1
    assert CountingRuleBased.calls == 5  # compare was not re-decided; cart and final were


def test_cancel_mid_loop_stops_at_the_next_boundary(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    user_id = uid(client, h)
    tid = agent_task(SessionLocal, user_id)
    calls = Calls()

    def user_cancels(ctx: ToolContext, n: int) -> None:
        with SessionLocal() as other:
            task_service.request_cancel(other, user_id, tid)

    calls.behaviour["evaluate_constraints"] = user_cancels
    assert loop_runner(SessionLocal, None, calls).run(tid) == "cancelled"
    t = fetch(client, h, tid)
    assert statuses(t) == [("search_catalog", "succeeded"), ("evaluate_constraints", "succeeded")]
    assert calls.count == Counter({"search_catalog": 1, "evaluate_constraints": 1})
    assert t["status"] == "cancelled" and t["resumable"] is False
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 409


def test_a_loop_that_loses_its_lease_stops_without_planning_again(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()

    def stolen(ctx: ToolContext, n: int) -> None:
        with SessionLocal() as other:
            row = other.get(AgentTask, tid)
            assert row is not None
            row.locked_by, row.locked_until = "new-runner", datetime.now(UTC) + timedelta(minutes=5)
            other.commit()

    calls.behaviour["search_catalog"] = stolen
    planner = CountingRuleBased()
    CountingRuleBased.calls = 0
    loop_runner(SessionLocal, planner, calls).run(tid)
    t = task_row(SessionLocal, tid)
    assert t.locked_by == "new-runner" and t.status == "running"
    assert calls.count == Counter({"search_catalog": 1}) and CountingRuleBased.calls == 1


def test_a_live_lease_blocks_a_second_loop_runner(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h))
    with SessionLocal() as s:
        row = s.get(AgentTask, tid)
        assert row is not None
        row.status, row.locked_by = "running", "busy-runner"
        row.locked_until = datetime.now(UTC) + timedelta(minutes=5)
        s.commit()
    planner = Scripted(Final())
    assert loop_runner(SessionLocal, planner).run(tid) is None and planner.calls == 0


# ------------------------------------------------------------------ API guarantees
def test_agent_tasks_are_idempotent_and_owned(client: TestClient) -> None:
    h, other = register(client), register(client)
    first = start_agent(client, h, idempotency_key="agent-key-0001")
    again = start_agent(client, h, idempotency_key="agent-key-0001")
    assert first.status_code == 201 and again.status_code == 200 and again.json()["id"] == first.json()["id"]
    assert start_agent(client, h, NO_MATCH, idempotency_key="agent-key-0001").status_code == 409
    tid = first.json()["id"]
    assert client.get(f"/api/tasks/{tid}", headers=other).status_code == 404
    assert client.post(f"/api/tasks/{tid}/cancel", headers=other).status_code == 404
    assert client.post(f"/api/tasks/{tid}/resume", headers=other).status_code == 404
    assert client.post("/api/tasks", json={"kind": "shopping_agent", "input": MOUSE}).status_code == 401
    listed = client.get("/api/tasks", headers=h).json()
    items = listed["items"] if isinstance(listed, dict) else listed
    assert [t["kind"] for t in items] == ["shopping_agent"]


def test_agent_tasks_never_write_outside_their_own_tables(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    assert client.post("/api/cart/items", json={"product_id": 1, "quantity": 1}, headers=h).status_code in (200, 201)
    watched = (Cart, CartItem, Order, AgentAction, Preference, Product)
    with SessionLocal() as s:
        before = counts(s, *watched)
        prices = list(s.scalars(select(Product.price_inr).order_by(Product.id)))
        stock = list(s.scalars(select(Product.stock).order_by(Product.id)))
    for inp in (MOUSE, NO_MATCH, {"goal": "add the best mouse to my cart", "allow_proposals": True}):
        assert fetch(client, h, start_agent(client, h, inp).json()["id"])["status"] == "succeeded"
    with SessionLocal() as s:
        # the only new row anywhere: one pending proposal from the opted-in task (S4); cart, orders,
        # preferences, prices and stock are untouched
        assert counts(s, *watched) == {**before, "agent_actions": before["agent_actions"] + 1}
        assert list(s.scalars(select(AgentAction.status))) == ["pending"]
        assert list(s.scalars(select(Product.price_inr).order_by(Product.id))) == prices
        assert list(s.scalars(select(Product.stock).order_by(Product.id))) == stock


def test_product_research_tasks_are_unchanged(client: TestClient) -> None:
    h = register(client)
    r = client.post("/api/tasks", json={"kind": "product_research", "input": {"category": "mice"}}, headers=h)
    t = fetch(client, h, r.json()["id"])
    assert t["status"] == "succeeded" and t["state"] is None
    assert [s["name"] for s in t["steps"]] == ["search", "compare", "cart", "summarize"]
    assert all(s["decision"] is None for s in t["steps"])


# ------------------------------------------------------------------ migration 0003
def _alembic(backend: pathlib.Path, env: dict[str, str], *args: str) -> str:
    r = subprocess.run(  # noqa: S603 - fixed argv (this interpreter, alembic, literal args); no shell
        [sys.executable, "-m", "alembic", *args], cwd=backend, env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout + r.stderr


def _migration_urls(tmp_path: pathlib.Path) -> list[str]:
    urls = [f"sqlite:///{tmp_path / 'migrate3.db'}"]
    if pg := os.environ.get("MIGRATION_TEST_DATABASE_URL"):  # a scratch Postgres database, never production
        urls.append(pg)
    return urls


def test_migration_0003_is_additive_and_keeps_existing_tasks(tmp_path: pathlib.Path) -> None:
    backend = pathlib.Path(__file__).resolve().parents[1]
    for url in _migration_urls(tmp_path):
        env = {**os.environ, "DATABASE_URL": url, "ENVIRONMENT": "test"}
        if not url.startswith("sqlite"):
            _alembic(backend, env, "downgrade", "base")  # the scratch database starts empty
        _alembic(backend, env, "upgrade", "0002")
        engine = create_engine(url)
        with engine.begin() as c:  # a Phase 2 task with one step, written before 0003 exists
            c.execute(
                text(
                    "INSERT INTO users (email, display_name, password_hash, created_at) "
                    "VALUES ('m3@example.com', 'M', 'x', CURRENT_TIMESTAMP)"
                )
            )
            user_id = c.execute(text("SELECT id FROM users WHERE email = 'm3@example.com'")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO agent_tasks (id, user_id, kind, goal, input, status, progress, current_step, "
                    "total_steps, cancel_requested, attempt, created_at, updated_at) VALUES ('t-0002', :u, "
                    "'product_research', 'g', '{}', 'succeeded', 100, 4, 4, false, 1, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                ),
                {"u": user_id},
            )
            c.execute(
                text(
                    "INSERT INTO agent_task_steps (task_id, position, name, tool, status, attempts, max_attempts) "
                    "VALUES ('t-0002', 0, 'search', 'search_catalog', 'succeeded', 1, 3)"
                )
            )
        _alembic(backend, env, "upgrade", "0003")
        assert "0003" in _alembic(backend, env, "current")
        _alembic(backend, env, "upgrade", "head")  # later migrations (0004+) build on 0003
        assert "No new upgrade operations detected" in _alembic(backend, env, "check")
        cols = {t: {c["name"]: c for c in inspect(engine).get_columns(t)} for t in ("agent_tasks", "agent_task_steps")}
        assert cols["agent_tasks"]["state"]["nullable"] and cols["agent_task_steps"]["decision"]["nullable"]
        with engine.connect() as c:
            assert c.execute(text("SELECT status, state FROM agent_tasks WHERE id = 't-0002'")).one() == (
                "succeeded",
                None,
            )
            assert c.execute(text("SELECT name, decision FROM agent_task_steps WHERE task_id = 't-0002'")).one() == (
                "search",
                None,
            )
        _alembic(backend, env, "downgrade", "0002")
        cols = {t: {c["name"] for c in inspect(engine).get_columns(t)} for t in ("agent_tasks", "agent_task_steps")}
        assert "state" not in cols["agent_tasks"] and "decision" not in cols["agent_task_steps"]
        with engine.connect() as c:  # downgrade keeps the existing rows
            assert c.execute(text("SELECT count(*) FROM agent_task_steps WHERE task_id = 't-0002'")).scalar_one() == 1
        _alembic(backend, env, "upgrade", "head")
        assert "No new upgrade operations detected" in _alembic(backend, env, "check")
        engine.dispose()


def test_search_bounds_are_enforced_before_validation_and_never_change_rule_based_searches(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    # a minimum above the task's budget: once held to ₹3,000 the search is impossible, so it is refused unrun
    tid = agent_task(SessionLocal, uid(client, h))
    calls = Calls()
    impossible = ToolCall(tool="search_catalog", args={"category": "mice", "min_price_inr": 5_000})
    assert loop_runner(SessionLocal, Scripted(impossible, Final()), calls).run(tid) == "succeeded"
    [rejected, _final] = fetch(client, h, tid)["steps"]
    assert rejected["status"] == "skipped" and rejected["error"]["code"] == "invalid_arguments"
    assert rejected["args"]["max_price_inr"] == 3_000 and calls.count == Counter()
    # the rule-based planner already searches within the task: nothing is clamped
    t = fetch(client, h, start_agent(client, h).json()["id"])
    assert t["status"] == "succeeded" and all("clamped" not in s["decision"] for s in t["steps"])
