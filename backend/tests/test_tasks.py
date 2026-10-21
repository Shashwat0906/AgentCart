"""Persistent agent tasks (Phase 2): schema, API, ownership, idempotency, cancellation, retries,
interruption and resume, leases, approval gating and the read-only guarantee."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.agents.tool_registry import READ_ONLY_TOOLS, ToolContext, ToolRegistry, build_read_only_registry
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.models import AgentAction, AgentTask, AgentTaskStep, Cart, CartItem, Order, Preference, Product
from app.services import tasks as task_service
from app.services.tasks import TaskRunner
from tests.conftest import register

LAPTOPS_UNDER_50K = {"category": "laptops", "max_price_inr": 50_000}


# ------------------------------------------------------------------ helpers
def uid(client: TestClient, h: dict[str, str]) -> int:
    return int(client.get("/api/auth/me", headers=h).json()["id"])


def start(client: TestClient, h: dict[str, str], inp: dict[str, Any] | None = None, **extra: Any) -> Any:
    return client.post(
        "/api/tasks", json={"kind": "product_research", "input": inp or LAPTOPS_UNDER_50K, **extra}, headers=h
    )


def fetch(client: TestClient, h: dict[str, str], task_id: str) -> dict[str, Any]:
    r = client.get(f"/api/tasks/{task_id}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def queued_task(SessionLocal: sessionmaker[Session], user_id: int, inp: dict[str, Any] | None = None) -> str:
    """Create a task without running it (the API would schedule a run)."""
    with SessionLocal() as s:
        task, _ = task_service.create_task(
            s, user_id, "product_research", inp or LAPTOPS_UNDER_50K, None, get_settings()
        )
        return task.id


class Calls:
    """Wraps registry tools to count executions and inject failures."""

    def __init__(self) -> None:
        self.count: Counter[str] = Counter()
        self.behaviour: dict[str, Any] = {}

    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        for spec in READ_ONLY_TOOLS:
            reg.register(dataclasses.replace(spec, handler=self._wrap(spec.name, spec.handler)))
        return reg

    def _wrap(self, name: str, real: Any) -> Any:
        def handler(ctx: ToolContext, inp: Any) -> dict[str, Any]:
            self.count[name] += 1
            hook = self.behaviour.get(name)
            if hook is not None:
                hook(ctx, self.count[name])
            return real(ctx, inp)

        return handler


def transient(times: int) -> Any:
    def hook(ctx: ToolContext, n: int) -> None:
        if n <= times:
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))

    return hook


def runner(
    SessionLocal: sessionmaker[Session], calls: Calls | None = None, sleeps: list[float] | None = None, **settings: Any
) -> TaskRunner:
    s = get_settings().model_copy(update=settings) if settings else get_settings()
    return TaskRunner(
        SessionLocal,
        s,
        registry_factory=calls.registry if calls else build_read_only_registry,
        sleep=(sleeps.append if sleeps is not None else (lambda _x: None)),
    )


def task_row(SessionLocal: sessionmaker[Session], task_id: str) -> AgentTask:
    s = SessionLocal()
    t = s.get(AgentTask, task_id)
    assert t is not None
    _ = list(t.steps)
    s.close()
    return t


def statuses(task: AgentTask | dict[str, Any]) -> list[tuple[str, str]]:
    steps = task["steps"] if isinstance(task, dict) else task.steps
    return [(s["name"], s["status"]) if isinstance(s, dict) else (s.name, s.status) for s in steps]


def counts(s: Session, *models: Any) -> dict[str, int]:
    return {m.__tablename__: int(s.scalar(select(func.count()).select_from(m)) or 0) for m in models}


# ------------------------------------------------------------------ schema and migration
def test_database_constraints(SessionLocal: sessionmaker[Session], client: TestClient) -> None:
    user_id = uid(client, register(client))
    tid = queued_task(SessionLocal, user_id)
    for change in ({"status": "bogus"}, {"progress": 101}, {"progress": -1}, {"attempt": -1}):
        with SessionLocal() as s:
            t = s.get(AgentTask, tid)
            for k, v in change.items():
                setattr(t, k, v)
            with pytest.raises(IntegrityError):
                s.commit()
    with SessionLocal() as s:
        s.add(AgentTaskStep(task_id=tid, position=0, name="dup", tool="search_catalog"))
        with pytest.raises(IntegrityError):  # (task_id, position) is unique
            s.commit()
    with SessionLocal() as s:
        s.add(AgentTaskStep(task_id=tid, position=9, name="x", tool="x", status="exploded"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_migration_0002_upgrades_downgrades_and_matches_the_models(tmp_path: pathlib.Path) -> None:
    backend = pathlib.Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'migrate.db'}", "ENVIRONMENT": "test"}

    def alembic(*args: str) -> str:
        r = subprocess.run(  # noqa: S603 - fixed argv (this interpreter, alembic, literal args); no shell
            [sys.executable, "-m", "alembic", *args], cwd=backend, env=env, capture_output=True, text=True
        )
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout + r.stderr

    alembic("upgrade", "0002")
    assert "0002" in alembic("current")
    alembic("upgrade", "head")  # later migrations (0003+) build on 0002
    assert "No new upgrade operations detected" in alembic("check")  # migrations == models
    alembic("downgrade", "0001")
    assert "0001" in alembic("current")
    alembic("upgrade", "head")


# ------------------------------------------------------------------ happy path through the API
def test_product_research_task_runs_to_completion(client: TestClient) -> None:
    h = register(client)
    r = start(client, h)
    assert r.status_code == 201 and r.json()["status"] == "queued" and r.json()["total_steps"] == 4
    t = fetch(client, h, r.json()["id"])  # the background run has completed by now
    assert t["status"] == "succeeded" and t["progress"] == 100 and t["current_step"] == 4
    assert statuses(t) == [
        ("search", "succeeded"),
        ("compare", "succeeded"),
        ("cart", "succeeded"),
        ("summarize", "succeeded"),
    ]
    assert all(s["attempts"] == 1 for s in t["steps"])
    assert t["goal"] == "Research laptops under ₹50,000"
    truth = client.get("/api/products", params={"category": "laptops", "max_price": 50_000, "in_stock": "true"}).json()
    res = t["result"]
    assert res["matches"] == truth["total"] == 4
    assert res["top"] and all(p["category"] == "laptops" and p["price_inr"] <= 50_000 for p in res["top"])
    assert res["all_within_budget"] is True and res["cart"] == {"item_count": 0, "total_inr": 0}
    assert set(res["best"]) == {"price_inr", "rating"}
    assert t["started_at"] and t["finished_at"] and t["attempt"] == 1 and t["error"] is None


def laptops_under(client: TestClient, cap: int) -> list[int]:
    """Ground truth from the catalogue API: in-stock laptops at or under `cap`."""
    params = {"category": "laptops", "max_price": cap, "in_stock": "true"}
    return sorted(p["id"] for p in client.get("/api/products", params=params).json()["items"])


def test_laptop_research_under_60k_finds_the_seeded_laptops(client: TestClient) -> None:
    """Regression: a ₹60,000 laptop research task must report the catalogue's matching laptops."""
    h = register(client)
    truth = laptops_under(client, 60_000)
    assert len(truth) == 7  # seeded catalogue
    t = fetch(client, h, start(client, h, {"category": "laptops", "max_price_inr": 60_000}).json()["id"])
    assert t["status"] == "succeeded" and t["result"]["matches"] == len(truth)
    assert all(p["category"] == "laptops" and p["price_inr"] <= 60_000 for p in t["result"]["top"])
    assert t["result"]["all_within_budget"] is True


@pytest.mark.parametrize(
    "inp",
    [
        {"query": "laptop under 60000"},
        {"query": "laptop under 60000", "max_price_inr": 60_000},
        {"query": "laptops under ₹60,000", "category": "laptops"},
        {"query": "laptop below 60k", "max_price_inr": 60_000},
        {"query": "Laptop for Rs 60,000"},
    ],
)
def test_a_budget_typed_into_the_query_is_a_price_limit_not_keywords(client: TestClient, inp: dict[str, Any]) -> None:
    """Every query word must match, so "under 60000" used to be searched for as text and found nothing
    (0 matches) although 7 laptops cost at most ₹60,000."""
    h = register(client)
    t = fetch(client, h, start(client, h, inp).json()["id"])
    assert t["status"] == "succeeded"
    search_args = t["steps"][0]["args"]
    assert search_args["query"].lower() in ("laptop", "laptops") and search_args["max_price_inr"] == 60_000
    assert t["result"]["matches"] == len(laptops_under(client, 60_000)) == 7
    assert all(p["price_inr"] <= 60_000 for p in t["result"]["top"]) and t["result"]["all_within_budget"]
    assert t["input"]["query"] == inp["query"]  # the task keeps what the user typed


def test_a_typed_budget_only_narrows_the_explicit_maximum(client: TestClient) -> None:
    h = register(client)
    lower = fetch(client, h, start(client, h, {"query": "laptop under 40000", "max_price_inr": 60_000}).json()["id"])
    assert lower["steps"][0]["args"]["max_price_inr"] == 40_000
    assert lower["result"]["matches"] == len(laptops_under(client, 40_000))
    higher = fetch(client, h, start(client, h, {"query": "laptop under 90000", "max_price_inr": 60_000}).json()["id"])
    assert higher["steps"][0]["args"]["max_price_inr"] == 60_000  # never widened
    assert higher["result"]["matches"] == 7


def test_a_typed_minimum_price_is_enforced(client: TestClient) -> None:
    h = register(client)
    t = fetch(client, h, start(client, h, {"query": "laptop above 60000"}).json()["id"])
    args = t["steps"][0]["args"]
    assert args["query"] == "laptop" and args["min_price_inr"] == 60_000 and "max_price_inr" not in args
    truth = client.get("/api/products", params={"category": "laptops", "min_price": 60_000, "in_stock": "true"}).json()
    assert t["result"]["matches"] == truth["total"] > 0
    assert all(p["price_inr"] >= 60_000 for p in t["result"]["top"]) and t["result"]["all_within_budget"] is True


def test_a_typed_budget_that_conflicts_with_the_maximum_price_is_rejected(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """ "above ₹60,000" with a ₹50,000 maximum can't both hold: reject it instead of dropping a limit
    and listing laptops the user excluded."""
    h = register(client)
    r = start(client, h, {"query": "laptop above 60000", "max_price_inr": 50_000})
    assert r.status_code == 422 and "conflicts with the maximum price" in r.json()["error"]["message"]
    with SessionLocal() as s:
        assert counts(s, AgentTask)["agent_tasks"] == 0
    # a range inside the maximum is fine and both ends are enforced
    t = fetch(
        client, h, start(client, h, {"query": "laptop between 40000 and 60000", "max_price_inr": 50_000}).json()["id"]
    )
    args = t["steps"][0]["args"]
    assert (args["min_price_inr"], args["max_price_inr"]) == (40_000, 50_000)
    assert all(40_000 <= p["price_inr"] <= 50_000 for p in t["result"]["top"])


def test_summary_budget_check_includes_a_typed_minimum() -> None:
    top = [
        {
            "id": 1,
            "name": "a",
            "brand": "b",
            "category": "laptops",
            "price_inr": 45_000,
            "rating": 4.0,
            "in_stock": True,
        }
    ]
    inp = {"query": "laptop above 60000", "compare_top": 3}
    summary = task_service._summarize(inp, {"search": {"items": top, "total": 1}})
    assert summary["all_within_budget"] is False


def test_queries_without_a_budget_are_searched_unchanged(client: TestClient) -> None:
    h = register(client)
    for inp in (
        {"query": "gaming laptop", "category": "laptops"},
        {"query": "noise cancelling"},
        {"query": "4k monitor"},
    ):
        t = fetch(client, h, start(client, h, inp).json()["id"])
        args = t["steps"][0]["args"]
        assert args["query"] == inp["query"] and "max_price_inr" not in args and "min_price_inr" not in args


def test_compare_is_skipped_when_fewer_than_two_products_match(client: TestClient) -> None:
    h = register(client)
    t = fetch(client, h, start(client, h, {"category": "laptops", "max_price_inr": 30_000}).json()["id"])
    assert t["status"] == "succeeded"
    assert statuses(t)[1] == ("compare", "skipped")
    assert "Only 1 matching product" in t["steps"][1]["output"]["reason"]
    assert t["result"]["matches"] == 1 and t["result"]["best"] == {}


def test_cart_step_is_optional_and_reads_the_owners_cart(client: TestClient) -> None:
    h = register(client)
    assert client.post("/api/cart/items", json={"product_id": 1, "quantity": 2}, headers=h).status_code in (200, 201)
    with_cart = fetch(client, h, start(client, h, {"category": "mice"}).json()["id"])
    assert with_cart["result"]["cart"]["item_count"] == 2
    no_cart = fetch(client, h, start(client, h, {"category": "mice", "include_cart": False}).json()["id"])
    assert [s["name"] for s in no_cart["steps"]] == ["search", "compare", "summarize"] and "cart" not in no_cart[
        "result"
    ]


def test_list_is_newest_first_and_omits_steps(client: TestClient) -> None:
    h = register(client)
    a = start(client, h, {"category": "mice"}).json()["id"]
    b = start(client, h, {"category": "keyboards"}).json()["id"]
    listed = client.get("/api/tasks", headers=h).json()
    assert [t["id"] for t in listed] == [b, a] and all(t["steps"] is None for t in listed)


# ------------------------------------------------------------------ validation: clients can't choose steps or tools
@pytest.mark.parametrize(
    "body",
    [
        {"kind": "buy_everything", "input": {"category": "laptops"}},
        {"kind": "product_research", "input": {"category": "laptops"}, "steps": [{"tool": "add_to_cart"}]},
        {"kind": "product_research", "input": {"category": "laptops", "tool": "add_to_cart"}},
        {"kind": "product_research", "input": {"category": "laptops", "max_price_inr": "50000"}},
        {"kind": "product_research", "input": {"category": "laptops", "compare_top": 5}},
        {"kind": "product_research", "input": {"category": "cars"}},
        {"kind": "product_research", "input": {}},  # needs a query or a category
        {"kind": "product_research", "input": "laptops"},
        {"kind": "product_research", "input": {"category": "laptops"}, "idempotency_key": "short"},
        {"kind": "product_research", "input": {"category": "laptops"}, "idempotency_key": "has spaces in it"},
    ],
)
def test_invalid_task_requests_are_rejected_and_nothing_is_created(
    client: TestClient, SessionLocal: sessionmaker[Session], body: dict[str, Any]
) -> None:
    h = register(client)
    r = client.post("/api/tasks", json=body, headers=h)
    assert r.status_code == 422, r.text
    assert "add_to_cart" not in r.text and "50000" not in r.text  # input is never echoed
    with SessionLocal() as s:
        assert counts(s, AgentTask)["agent_tasks"] == 0


# ------------------------------------------------------------------ authentication and ownership
def test_all_task_endpoints_require_sign_in(client: TestClient) -> None:
    for method, path in [
        ("POST", "/api/tasks"),
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/x"),
        ("POST", "/api/tasks/x/cancel"),
        ("POST", "/api/tasks/x/resume"),
    ]:
        assert client.request(method, path, json={}).status_code == 401, path


def test_users_cannot_see_or_control_each_others_tasks(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    tid = start(client, alice).json()["id"]
    for method, path in [
        ("GET", f"/api/tasks/{tid}"),
        ("POST", f"/api/tasks/{tid}/cancel"),
        ("POST", f"/api/tasks/{tid}/resume"),
    ]:
        assert client.request(method, path, headers=bob).status_code == 404, path
    assert client.get("/api/tasks", headers=bob).json() == []
    assert [t["id"] for t in client.get("/api/tasks", headers=alice).json()] == [tid]


def test_read_cart_step_uses_the_task_owner_not_the_caller(client: TestClient) -> None:
    alice, bob = register(client), register(client)
    client.post("/api/cart/items", json={"product_id": 1, "quantity": 3}, headers=alice)
    t = fetch(client, bob, start(client, bob, {"category": "mice"}).json()["id"])
    assert t["result"]["cart"]["item_count"] == 0  # bob's own (empty) cart, never alice's


# ------------------------------------------------------------------ idempotency and limits
def test_same_idempotency_key_returns_the_original_task(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    first = start(client, h, idempotency_key="research-0001")
    again = start(client, h, idempotency_key="research-0001")
    assert (first.status_code, again.status_code) == (201, 200) and first.json()["id"] == again.json()["id"]
    with SessionLocal() as s:
        assert counts(s, AgentTask)["agent_tasks"] == 1
    assert all(st["attempts"] == 1 for st in fetch(client, h, first.json()["id"])["steps"])  # ran once


def test_idempotency_key_reused_for_different_input_conflicts(client: TestClient) -> None:
    h = register(client)
    assert start(client, h, idempotency_key="research-0002").status_code == 201
    r = start(client, h, {"category": "mice"}, idempotency_key="research-0002")
    assert r.status_code == 409


def test_idempotency_keys_are_per_user(client: TestClient) -> None:
    a, b = register(client), register(client)
    ta = start(client, a, idempotency_key="shared-key-01").json()["id"]
    tb = start(client, b, idempotency_key="shared-key-01").json()["id"]
    assert ta != tb


def test_active_task_limit(
    client: TestClient, SessionLocal: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "task_max_active_per_user", 2)
    h = register(client)
    user_id = uid(client, h)
    queued_task(SessionLocal, user_id)
    queued_task(SessionLocal, user_id)
    r = start(client, h)
    assert r.status_code == 429 and r.json()["error"]["details"] == {"active": 2, "limit": 2}


# ------------------------------------------------------------------ cancellation
def test_cancelling_a_queued_task_takes_effect_immediately(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    r = client.post(f"/api/tasks/{tid}/cancel", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["finished_at"]
    assert {st for _, st in statuses(r.json())} == {"cancelled"}
    assert client.post(f"/api/tasks/{tid}/cancel", headers=h).status_code == 409
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 409
    assert runner(SessionLocal).run(tid) is None  # a stale scheduled run does nothing


def test_cancel_during_a_run_stops_at_the_next_step_boundary(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    calls = Calls()

    def user_cancels(ctx: ToolContext, n: int) -> None:  # the user presses Cancel while search runs
        with SessionLocal() as other:
            task_service.request_cancel(other, uid(client, h), tid)

    calls.behaviour["search_catalog"] = user_cancels
    assert runner(SessionLocal, calls).run(tid) == "cancelled"
    t = task_row(SessionLocal, tid)
    assert statuses(t) == [
        ("search", "succeeded"),
        ("compare", "cancelled"),
        ("cart", "cancelled"),
        ("summarize", "cancelled"),
    ]
    assert calls.count == Counter({"search_catalog": 1})  # nothing after the boundary ran
    assert t.status == "cancelled" and t.locked_by is None


def test_finished_tasks_cannot_be_cancelled(client: TestClient) -> None:
    h = register(client)
    tid = start(client, h).json()["id"]
    r = client.post(f"/api/tasks/{tid}/cancel", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["status"] == "succeeded"


# ------------------------------------------------------------------ retries and recovery
def test_transient_failures_are_retried_with_exponential_backoff(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    calls, sleeps = Calls(), list[float]()
    calls.behaviour["search_catalog"] = transient(2)
    assert runner(SessionLocal, calls, sleeps, task_retry_backoff_seconds=0.5).run(tid) == "succeeded"
    t = task_row(SessionLocal, tid)
    assert t.steps[0].attempts == 3 and t.steps[0].status == "succeeded" and t.steps[0].error is None
    assert sleeps == [0.5, 1.0]


def test_exhausted_retries_fail_as_retryable_and_resume_skips_finished_steps(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    calls = Calls()
    calls.behaviour["compare_products"] = transient(99)
    assert runner(SessionLocal, calls).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert (
        t["error"]["code"] == "temporarily_unavailable"
        and t["error"]["retryable"] is True
        and t["error"]["step"] == "compare"
    )
    assert statuses(t)[:2] == [("search", "succeeded"), ("compare", "failed")] and t["steps"][1]["attempts"] == 3
    assert t["resumable"] is True
    frozen_args = t["steps"][1]["args"]
    search_before = t["steps"][0]

    # The outage is over; the API's resume runs the task with the normal registry.
    r = client.post(f"/api/tasks/{tid}/resume", headers=h)
    assert r.status_code == 200
    t = fetch(client, h, tid)
    assert t["status"] == "succeeded" and t["attempt"] == 2 and t["error"] is None
    assert t["steps"][1]["args"] == frozen_args  # the resumed step replayed exactly the same call
    assert t["steps"][1]["attempts"] == 1  # reset on resume, then one successful attempt
    # The search step finished before the failure: its stored record is untouched, i.e. not re-executed.
    assert t["steps"][0] == search_before


def test_resume_reuses_stored_output_and_runs_only_unfinished_steps(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    calls = Calls()
    calls.behaviour["compare_products"] = transient(99)
    runner(SessionLocal, calls).run(tid)
    with SessionLocal() as s:
        task_service.prepare_resume(s, uid(client, h), tid, get_settings())
    calls.behaviour.clear()
    assert runner(SessionLocal, calls).run(tid) == "succeeded"
    assert calls.count["search_catalog"] == 1 and calls.count["compare_products"] == 3 + 1


def test_permanent_failures_are_not_retried_or_resumable(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    calls = Calls()

    def gone(ctx: ToolContext, n: int) -> None:
        raise NotFoundError("One or more products do not exist")

    calls.behaviour["compare_products"] = gone
    assert runner(SessionLocal, calls).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"] == {
        "code": "not_found",
        "message": "One or more products do not exist",
        "retryable": False,
        "step": "compare",
    }
    assert t["steps"][1]["attempts"] == 1 and t["resumable"] is False
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 409


# ------------------------------------------------------------------ interruption (crash, restart, deploy)
class _Crash(BaseException):
    """Simulates the process dying mid-step (not a normal exception: nothing gets to clean up)."""


def test_interrupted_task_is_detected_and_resumes_without_repeating_finished_steps(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    calls = Calls()

    def die(ctx: ToolContext, n: int) -> None:
        raise _Crash

    calls.behaviour["compare_products"] = die
    with pytest.raises(_Crash):
        runner(SessionLocal, calls).run(tid)
    t = task_row(SessionLocal, tid)
    assert t.status == "running" and statuses(t)[:2] == [("search", "succeeded"), ("compare", "running")]
    assert t.steps[1].attempts == 1  # recorded before the call
    assert fetch(client, h, tid)["interrupted"] is False  # lease still live: not yet considered dead

    with SessionLocal() as s:  # time passes: the dead runner's lease expires
        row = s.get(AgentTask, tid)
        assert row is not None
        row.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    view = fetch(client, h, tid)
    assert view["interrupted"] is True and view["resumable"] is True

    calls.behaviour.clear()
    assert runner(SessionLocal, calls).run(tid) == "succeeded"  # a new runner may claim it directly
    t = task_row(SessionLocal, tid)
    assert calls.count["search_catalog"] == 1  # finished before the crash: never repeated
    assert t.steps[1].attempts == 2 and t.attempt == 2 and t.status == "succeeded"


def test_interrupted_task_can_be_resumed_through_the_api(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    with SessionLocal() as s:
        row = s.get(AgentTask, tid)
        assert row is not None
        row.status, row.locked_by = "running", "dead-runner"
        row.locked_until = datetime.now(UTC) - timedelta(minutes=5)
        s.commit()
    r = client.post(f"/api/tasks/{tid}/resume", headers=h)
    assert r.status_code == 200
    assert fetch(client, h, tid)["status"] == "succeeded"


def test_a_live_lease_blocks_a_second_runner_and_resume(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    with SessionLocal() as s:
        row = s.get(AgentTask, tid)
        assert row is not None
        row.status, row.locked_by = "running", "busy-runner"
        row.locked_until = datetime.now(UTC) + timedelta(minutes=5)
        s.commit()
    assert runner(SessionLocal).run(tid) is None
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 409
    t = task_row(SessionLocal, tid)
    assert t.locked_by == "busy-runner" and {st for _, st in statuses(t)} == {"queued"}


def test_a_runner_that_loses_its_lease_stops_without_writing(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    calls = Calls()

    def stolen(ctx: ToolContext, n: int) -> None:  # our lease expired and another runner took over
        with SessionLocal() as other:
            row = other.get(AgentTask, tid)
            assert row is not None
            row.locked_by, row.locked_until = "new-runner", datetime.now(UTC) + timedelta(minutes=5)
            other.commit()

    calls.behaviour["search_catalog"] = stolen
    runner(SessionLocal, calls).run(tid)
    t = task_row(SessionLocal, tid)
    assert t.locked_by == "new-runner" and t.status == "running"
    assert calls.count == Counter({"search_catalog": 1})  # stopped at the next boundary


# ------------------------------------------------------------------ approval gating and read-only guarantee
def test_runner_refuses_tools_that_are_not_read_only(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = queued_task(SessionLocal, uid(client, h))
    with SessionLocal() as s:  # even if a step somehow named a cart mutation …
        step = s.scalar(select(AgentTaskStep).where(AgentTaskStep.task_id == tid, AgentTaskStep.position == 0))
        assert step is not None
        step.tool, step.args = "add_to_cart", {"product_id": 1, "quantity": 1}
        s.commit()
    assert runner(SessionLocal).run(tid) == "failed"
    t = fetch(client, h, tid)
    assert t["error"]["code"] == "unknown_tool" and t["error"]["retryable"] is False
    with SessionLocal() as s:
        assert counts(s, CartItem)["cart_items"] == 0  # … nothing reached the cart


def test_tasks_never_write_outside_their_own_tables(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    watched = (Cart, CartItem, Order, AgentAction, Preference, Product)
    with SessionLocal() as s:
        before = counts(s, *watched)
        prices_before = list(s.scalars(select(Product.price_inr).order_by(Product.id)))
    for inp in ({"category": "laptops"}, {"query": "noise cancelling"}, {"category": "mice", "include_cart": False}):
        assert fetch(client, h, start(client, h, inp).json()["id"])["status"] == "succeeded"
    with SessionLocal() as s:
        assert counts(s, *watched) == before
        assert list(s.scalars(select(Product.price_inr).order_by(Product.id))) == prices_before


def test_registry_reports_database_outages_as_retryable(db: Session) -> None:
    calls = Calls()
    calls.behaviour["get_product"] = transient(1)
    r = calls.registry().execute("get_product", {"product_id": 1}, ToolContext(db=db, settings=get_settings()))
    assert r.error is not None and r.error.code == "temporarily_unavailable"
    assert "connection reset" not in repr(r)


# ------------------------------------------------------------------ backward compatibility
def test_chat_and_cart_flows_are_unaffected(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    r = client.post("/api/chat", json={"message": "laptop under ₹70,000 with 16GB RAM"}, headers=h)
    assert r.status_code == 200 and r.json()["assistant_message"]["payload"]["recommendations"]
    with SessionLocal() as s:
        assert counts(s, AgentTask)["agent_tasks"] == 0  # chat never creates tasks


def test_default_task_settings_are_safe() -> None:
    s = Settings(_env_file=None, environment="test")
    assert (
        s.task_step_max_attempts,
        s.task_retry_backoff_seconds,
        s.task_lease_seconds,
        s.task_max_active_per_user,
    ) == (3, 0.5, 120, 5)
