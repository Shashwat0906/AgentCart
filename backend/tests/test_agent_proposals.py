"""Phase 3 S4: approval-gated cart proposals from agent tasks.

An opted-in `shopping_agent` task may create one *pending* add-to-cart action. Nothing reaches the cart
until its owner approves it through the approval API, which re-checks price and stock. A resumed or retried
step never proposes twice.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.agents.planner import Final, PlannerState, ToolCall
from app.agents.tool_registry import (
    PROPOSE_TOOLS,
    READ_ONLY_TOOLS,
    ProposeAddToCartInput,
    ReadCartInput,
    TaskConstraints,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    build_task_registry,
)
from app.core.config import get_settings
from app.models import AgentAction, AgentTask, AgentTaskStep, Cart, CartItem, Order, Preference, Product
from app.services import tasks as task_service
from app.services.tasks import TaskRunner
from tests.conftest import register
from tests.test_agent_loop import Scripted, agent_task, start_agent
from tests.test_tasks import _Crash, counts, fetch, task_row, transient, uid

ADD_MOUSE = {"goal": "add a wireless mouse under ₹3,000 to my cart", "allow_proposals": True}


# ------------------------------------------------------------------ helpers
class Calls:
    """The task registry (read + propose tools) with execution counts and injectable behaviour."""

    def __init__(self) -> None:
        self.count: Counter[str] = Counter()
        self.behaviour: dict[str, Any] = {}

    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        for spec in (*READ_ONLY_TOOLS, *PROPOSE_TOOLS):
            reg.register(dataclasses.replace(spec, handler=self._wrap(spec.name, spec.handler)))
        return reg

    def _wrap(self, name: str, real: Any) -> Any:
        def handler(ctx: ToolContext, inp: Any) -> dict[str, Any]:
            self.count[name] += 1
            out = real(ctx, inp)
            hook = self.behaviour.get(name)
            if hook is not None:
                hook(ctx, self.count[name])
            return out

        return handler


def runner(
    SessionLocal: sessionmaker[Session], calls: Calls | None = None, planner: Any = None, **settings: Any
) -> TaskRunner:
    s = get_settings().model_copy(update=settings) if settings else get_settings()
    kwargs: dict[str, Any] = {"sleep": lambda _x: None}
    if calls is not None:
        kwargs["registry_factory"] = calls.registry
    if planner is not None:
        kwargs["planner_factory"] = lambda: planner
    return TaskRunner(SessionLocal, s, **kwargs)


def actions(SessionLocal: sessionmaker[Session], task_id: str | None = None) -> list[AgentAction]:
    with SessionLocal() as s:
        stmt = select(AgentAction).order_by(AgentAction.created_at)
        if task_id is not None:
            stmt = stmt.where(AgentAction.task_id == task_id)
        return list(s.scalars(stmt))


def cart_lines(client: TestClient, h: dict[str, str]) -> list[tuple[int, int]]:
    return [(i["product_id"], i["quantity"]) for i in client.get("/api/cart", headers=h).json()["items"]]


def mice_under(client: TestClient, cap: int) -> dict[int, dict[str, Any]]:
    params = {"category": "mice", "max_price": cap, "in_stock": "true"}
    return {p["id"]: p for p in client.get("/api/products", params=params).json()["items"]}


# ------------------------------------------------------------------ the proposal flow end to end
def test_opted_in_task_proposes_once_and_nothing_changes_until_approval(client: TestClient) -> None:
    h = register(client)
    t = fetch(client, h, start_agent(client, h, ADD_MOUSE).json()["id"])
    assert t["status"] == "succeeded"  # D2: research done, proposal listed, approval is separate
    assert [s["tool"] for s in t["steps"]] == [
        "search_catalog",
        "evaluate_constraints",
        "compare_products",
        "read_cart",
        "propose_add_to_cart",
        "final",
    ]
    step = t["steps"][4]
    assert step["status"] == "succeeded" and step["output"]["reused"] is False
    [proposal] = t["proposals"]
    assert proposal["status"] == "pending" and proposal["task_id"] == t["id"] and proposal["kind"] == "add_to_cart"
    assert t["result"]["proposals"] == [
        {
            k: step["output"][k]
            for k in ("action_id", "product_id", "product_name", "quantity", "unit_price_inr", "summary")
        }
    ]
    # the proposed product passed the task's own constraint check and is a real in-budget mouse
    eligible = {r["product_id"] for r in t["steps"][1]["output"]["results"] if r["passes"]}
    pid = proposal["payload"]["product_id"]
    assert pid in eligible and pid in mice_under(client, 3_000)
    assert proposal["payload"]["expected_price_inr"] == mice_under(client, 3_000)[pid]["price_inr"] <= 3_000
    assert cart_lines(client, h) == []  # nothing until approval

    r = client.post(f"/api/actions/{proposal['id']}/approve", headers=h)
    assert r.status_code == 200 and r.json()["action"]["status"] == "executed"
    assert cart_lines(client, h) == [(pid, 1)]
    assert client.post(f"/api/actions/{proposal['id']}/approve", headers=h).status_code == 410  # idempotent
    assert cart_lines(client, h) == [(pid, 1)]
    assert [p["status"] for p in fetch(client, h, t["id"])["proposals"]] == ["executed"]  # live status


def test_rejecting_a_task_proposal_leaves_the_cart_untouched(client: TestClient) -> None:
    h = register(client)
    [proposal] = fetch(client, h, start_agent(client, h, ADD_MOUSE).json()["id"])["proposals"]
    assert client.post(f"/api/actions/{proposal['id']}/reject", headers=h).json()["action"]["status"] == "rejected"
    assert client.post(f"/api/actions/{proposal['id']}/approve", headers=h).status_code == 410
    assert cart_lines(client, h) == []


def test_no_proposal_without_opt_in_or_when_disabled(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    not_opted = fetch(client, h, start_agent(client, h, {**ADD_MOUSE, "allow_proposals": False}).json()["id"])
    assert "propose_add_to_cart" not in {s["tool"] for s in not_opted["steps"]} and not_opted["proposals"] == []
    assert "weren't enabled" in not_opted["result"]["notes"]
    tid = agent_task(SessionLocal, uid(client, h), ADD_MOUSE)
    offered: list[set[str]] = []

    class Spy(Scripted):
        def decide(self, state: PlannerState, tools: Any) -> Any:
            offered.append({str(t["name"]) for t in tools})
            return super().decide(state, tools)

    runner(SessionLocal, planner=Spy(Final()), task_max_proposals=0).run(tid)
    assert "propose_add_to_cart" not in offered[0]  # disabled by configuration
    assert actions(SessionLocal) == []


def test_research_tasks_can_never_propose(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    """product_research steps run with read permission only, even with the task registry."""
    h = register(client)
    with SessionLocal() as s:
        tid = task_service.create_task(
            s, uid(client, h), "product_research", {"category": "mice"}, None, get_settings()
        )[0].id
    with SessionLocal() as s:
        step = s.scalar(select(AgentTaskStep).where(AgentTaskStep.task_id == tid, AgentTaskStep.position == 0))
        assert step is not None
        step.tool, step.args = "propose_add_to_cart", {"product_id": 1, "quantity": 1}
        s.commit()
    assert runner(SessionLocal).run(tid) == "failed"
    assert fetch(client, h, tid)["error"]["code"] == "forbidden"
    assert actions(SessionLocal) == [] and cart_lines(client, h) == []


# ------------------------------------------------------------------ refused proposals
def test_a_planner_cannot_propose_an_ineligible_product(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h), ADD_MOUSE)
    expensive = max(
        client.get("/api/products", params={"category": "mice"}).json()["items"], key=lambda p: p["price_inr"]
    )
    assert expensive["price_inr"] > 3_000
    planner = Scripted(
        ToolCall(tool="propose_add_to_cart", args={"product_id": expensive["id"], "quantity": 1}), Final()
    )
    assert runner(SessionLocal, planner=planner).run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert t["steps"][0]["status"] == "skipped" and t["steps"][0]["error"]["code"] == "ungrounded_proposal"
    assert actions(SessionLocal) == [] and cart_lines(client, h) == []


def _ctx(db: Session, user_id: int, task_id: str, step_id: int, req: TaskConstraints, **kw: Any) -> ToolContext:
    return ToolContext(
        db=db,
        settings=get_settings().model_copy(update=kw) if kw else get_settings(),
        user_id=user_id,
        allowed_permissions=frozenset({"read", "propose"}),
        constraints=req,
        task_id=task_id,
        task_step_id=step_id,
    )


def _step(SessionLocal: sessionmaker[Session], task_id: str, position: int) -> int:
    with SessionLocal() as s:
        st = AgentTaskStep(task_id=task_id, position=position, name="propose_add_to_cart", tool="propose_add_to_cart")
        s.add(st)
        s.commit()
        return st.id


MICE_UNDER_3K = TaskConstraints(
    category="mice",
    hard=tuple(
        task_service.TaskConstraints.from_intent(
            task_service.derive_intent(task_service.AgentGoal(goal="a mouse under ₹3,000"), max_item_quantity=10)[0]
        ).hard
    ),
)


def test_the_tool_itself_rechecks_eligibility_stock_quantity_and_the_cap(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """Even a caller that skips the planner's checks can't get an ineligible proposal stored."""
    h = register(client)
    user_id = uid(client, h)
    tid = agent_task(SessionLocal, user_id, ADD_MOUSE)
    reg = build_task_registry()
    cheap = min(mice_under(client, 3_000).values(), key=lambda p: p["price_inr"])
    expensive = max(
        client.get("/api/products", params={"category": "mice"}).json()["items"], key=lambda p: p["price_inr"]
    )
    laptop = client.get("/api/products", params={"category": "laptops"}).json()["items"][0]

    def propose(args: dict[str, Any], position: int, **kw: Any) -> Any:
        step_id = _step(SessionLocal, tid, position)
        with SessionLocal() as s:
            res = reg.execute("propose_add_to_cart", args, _ctx(s, user_id, tid, step_id, MICE_UNDER_3K, **kw))
            s.commit()
            return res

    over_budget = propose({"product_id": expensive["id"]}, 0)
    assert over_budget.error is not None and over_budget.error.code == "invalid_request"
    assert "doesn't meet the task's requirements" in over_budget.error.message
    wrong_category = propose({"product_id": laptop["id"]}, 1)
    assert wrong_category.error is not None and "Category" in wrong_category.error.message
    too_many = propose({"product_id": cheap["id"], "quantity": 11}, 2)
    assert too_many.error is not None and "at most 10 units" in too_many.error.message
    with SessionLocal() as s:
        s.get(Product, cheap["id"]).stock = 0  # type: ignore[union-attr]
        s.commit()
    out_of_stock = propose({"product_id": cheap["id"]}, 3)
    assert out_of_stock.error is not None and "Out of stock" in out_of_stock.error.message
    with SessionLocal() as s:
        s.get(Product, cheap["id"]).stock = 2  # type: ignore[union-attr]
        s.commit()
    short = propose({"product_id": cheap["id"], "quantity": 3}, 7)
    assert short.error is not None and "Only 2 unit(s)" in short.error.message
    with SessionLocal() as s:
        s.get(Product, cheap["id"]).stock = 50  # type: ignore[union-attr]
        s.commit()
    ok = propose({"product_id": cheap["id"]}, 4)
    assert ok.ok and ok.data is not None and ok.data["unit_price_inr"] == cheap["price_inr"]
    capped = propose({"product_id": cheap["id"]}, 5)  # TASK_MAX_PROPOSALS=1
    assert capped.error is not None and "at most 1 cart proposal" in capped.error.message
    assert [a.status for a in actions(SessionLocal, tid)] == ["pending"]
    assert cart_lines(client, h) == []

    with SessionLocal() as s:  # strict input: price, owner and checks are never arguments
        bad = reg.execute(
            "propose_add_to_cart",
            {"product_id": cheap["id"], "expected_price_inr": 1, "user_id": 999},
            _ctx(s, user_id, tid, _step(SessionLocal, tid, 6), MICE_UNDER_3K),
        )
        assert bad.error is not None and bad.error.code == "invalid_input"
        outside_task = reg.execute(
            "propose_add_to_cart",
            {"product_id": cheap["id"]},
            ToolContext(
                db=s, settings=get_settings(), user_id=user_id, allowed_permissions=frozenset({"read", "propose"})
            ),
        )
        assert outside_task.error is not None and "only be made by a task step" in outside_task.error.message
        read_only = reg.execute(
            "propose_add_to_cart",
            {"product_id": cheap["id"]},
            ToolContext(db=s, settings=get_settings(), user_id=user_id),
        )
        assert read_only.error is not None and read_only.error.code == "forbidden"


def test_a_propose_tool_can_only_create_pending_actions(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """The registry rolls back anything a propose tool does besides adding pending actions."""
    h = register(client)
    user_id = uid(client, h)
    tid = agent_task(SessionLocal, user_id, ADD_MOUSE)

    def sneaky(ctx: ToolContext, inp: Any) -> dict[str, Any]:
        ctx.db.get(Product, 1).price_inr = 1  # type: ignore[union-attr]
        return {}

    def executes_itself(ctx: ToolContext, inp: Any) -> dict[str, Any]:
        ctx.db.add(
            AgentAction(
                id="x" * 36,
                user_id=ctx.user_id,
                kind="add_to_cart",
                payload={},
                summary="s",
                status="executed",
                result={},
                expires_at=datetime.now(UTC),
            )
        )
        return {}

    reg = ToolRegistry()
    for name, handler in (("propose_sneaky", sneaky), ("propose_executed", executes_itself)):
        reg.register(
            ToolSpec(
                name=name,
                description="x",
                input_model=ReadCartInput,
                output_model=ReadCartInput.__base__,  # type: ignore[arg-type]
                handler=handler,
                permission="propose",
            )
        )
    with SessionLocal() as s:
        price = s.get(Product, 1).price_inr  # type: ignore[union-attr]
        for name in ("propose_sneaky", "propose_executed"):
            res = reg.execute(
                name,
                {},
                _ctx(s, user_id, tid, _step(SessionLocal, tid, 0 if name.endswith("sneaky") else 1), MICE_UNDER_3K),
            )
            assert res.error is not None and res.error.code == "write_attempted", name
        s.commit()
    with SessionLocal() as s:
        assert s.get(Product, 1).price_inr == price  # type: ignore[union-attr]
        assert s.scalar(select(func.count()).select_from(AgentAction)) == 0


# ------------------------------------------------------------------ never twice: resume, retry, crash
def test_a_resumed_propose_step_returns_its_proposal_instead_of_proposing_again(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """The runner dies after the proposal was stored but before the step was marked done. On resume the
    step runs again (finished steps never do; this one hadn't finished) and gets the same proposal back."""
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h), ADD_MOUSE)
    calls = Calls()

    def die_after_storing(ctx: ToolContext, n: int) -> None:
        if n == 1:
            ctx.db.commit()  # the proposal is durable …
            raise _Crash  # … and then the process dies

    calls.behaviour["propose_add_to_cart"] = die_after_storing
    with pytest.raises(_Crash):
        runner(SessionLocal, calls).run(tid)
    t = task_row(SessionLocal, tid)
    assert t.steps[-1].tool == "propose_add_to_cart" and t.steps[-1].status == "running"
    [first] = actions(SessionLocal, tid)
    assert first.task_step_id == t.steps[-1].id

    with SessionLocal() as s:  # the dead runner's lease expires
        row = s.get(AgentTask, tid)
        assert row is not None
        row.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    assert runner(SessionLocal, calls).run(tid) == "succeeded"
    t2 = fetch(client, h, tid)
    assert [a.id for a in actions(SessionLocal, tid)] == [first.id]  # still exactly one proposal
    propose_steps = [s for s in t2["steps"] if s["tool"] == "propose_add_to_cart"]
    assert len(propose_steps) == 1 and propose_steps[0]["output"]["reused"] is True
    assert propose_steps[0]["output"]["action_id"] == first.id and propose_steps[0]["attempts"] == 2
    assert [p["action_id"] for p in t2["result"]["proposals"]] == [first.id]
    assert cart_lines(client, h) == []


def test_transient_failures_in_the_propose_step_are_retried_without_duplicates(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h), ADD_MOUSE)
    calls = Calls()
    fail_twice = transient(2)
    calls.behaviour["propose_add_to_cart"] = fail_twice  # raised after the handler added the action
    assert runner(SessionLocal, calls).run(tid) == "succeeded"
    assert calls.count["propose_add_to_cart"] == 3
    assert len(actions(SessionLocal, tid)) == 1  # failed attempts were rolled back
    assert cart_lines(client, h) == []


def test_a_retryable_failure_resumes_without_a_second_proposal(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h), ADD_MOUSE)
    calls = Calls()
    calls.behaviour["propose_add_to_cart"] = transient(99)
    assert runner(SessionLocal, calls).run(tid) == "failed"
    assert fetch(client, h, tid)["resumable"] is True and actions(SessionLocal, tid) == []
    calls.behaviour.clear()
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 200
    assert fetch(client, h, tid)["status"] == "succeeded"
    assert len(actions(SessionLocal, tid)) == 1


def test_the_database_allows_one_action_per_step(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    user_id = uid(client, h)
    tid = agent_task(SessionLocal, user_id, ADD_MOUSE)
    step_id = _step(SessionLocal, tid, 0)

    def action(i: int) -> AgentAction:
        return AgentAction(
            id=f"a-{i}",
            user_id=user_id,
            task_id=tid,
            task_step_id=step_id,
            kind="add_to_cart",
            payload={},
            summary="s",
            status="pending",
            result={},
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

    with SessionLocal() as s:
        s.add(action(1))
        s.commit()
        s.add(action(2))
        with pytest.raises(IntegrityError):
            s.commit()


# ------------------------------------------------------------------ approval revalidates price and stock
def _proposal(client: TestClient, h: dict[str, str]) -> dict[str, Any]:
    [p] = fetch(client, h, start_agent(client, h, ADD_MOUSE).json()["id"])["proposals"]
    return p


def test_approval_rechecks_the_price_and_keeps_the_task_link(client: TestClient, db: Session) -> None:
    h = register(client)
    p = _proposal(client, h)
    product = db.get(Product, p["payload"]["product_id"])
    assert product is not None
    product.price_inr += 200
    db.commit()
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "price_changed"
    assert cart_lines(client, h) == []
    fresh = r.json()["error"]["details"]["new_action_id"]
    statuses = {a["id"]: (a["status"], a["task_id"]) for a in fetch(client, h, p["task_id"])["proposals"]}
    assert statuses == {p["id"]: ("superseded", p["task_id"]), fresh: ("pending", p["task_id"])}
    ok = client.post(f"/api/actions/{fresh}/approve", headers=h)
    assert ok.status_code == 200 and ok.json()["cart"]["items"][0]["unit_price_inr"] == product.price_inr


def test_approval_rechecks_stock(client: TestClient, db: Session) -> None:
    h = register(client)
    p = _proposal(client, h)
    product = db.get(Product, p["payload"]["product_id"])
    assert product is not None
    product.stock = 0
    db.commit()
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "cart_rejected"
    assert [a["status"] for a in fetch(client, h, p["task_id"])["proposals"]] == ["failed"]
    assert cart_lines(client, h) == []


def test_expired_and_foreign_proposals_cannot_be_approved(client: TestClient, db: Session) -> None:
    h, other = register(client), register(client)
    p = _proposal(client, h)
    assert client.post(f"/api/actions/{p['id']}/approve", headers=other).status_code == 404
    assert client.get(f"/api/tasks/{p['task_id']}", headers=other).status_code == 404
    row = db.get(AgentAction, p["id"])
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 410 and r.json()["error"]["details"]["status"] == "expired"
    assert cart_lines(client, h) == []


def test_there_is_still_no_checkout_or_execute_capability() -> None:
    reg = build_task_registry()
    perms = {reg.get(n).permission for n in reg.names()}  # type: ignore[union-attr]
    assert perms == {"read", "propose"}
    assert not any(w in n for n in reg.names() for w in ("checkout", "order", "pay", "execute", "approve"))
    assert set(ProposeAddToCartInput.model_fields) == {"product_id", "quantity"}


def test_agent_tasks_write_only_pending_actions(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    watched = (Cart, CartItem, Order, Preference, Product)
    with SessionLocal() as s:
        before = counts(s, *watched)
        prices = list(s.scalars(select(Product.price_inr).order_by(Product.id)))
        stock = list(s.scalars(select(Product.stock).order_by(Product.id)))
    for _ in range(2):
        assert fetch(client, h, start_agent(client, h, ADD_MOUSE).json()["id"])["status"] == "succeeded"
    with SessionLocal() as s:
        assert counts(s, *watched) == before
        assert list(s.scalars(select(Product.price_inr).order_by(Product.id))) == prices
        assert list(s.scalars(select(Product.stock).order_by(Product.id))) == stock
        assert list(s.scalars(select(AgentAction.status))) == ["pending", "pending"]  # one per task


def test_task_list_includes_live_proposals(client: TestClient) -> None:
    h = register(client)
    tid = start_agent(client, h, ADD_MOUSE).json()["id"]
    listed = {t["id"]: t for t in client.get("/api/tasks", headers=h).json()}
    assert [p["status"] for p in listed[tid]["proposals"]] == ["pending"]


# ------------------------------------------------------------------ the task budget at approval time
def _set_price(db: Session, product_id: int, price: int) -> None:
    product = db.get(Product, product_id)
    assert product is not None
    product.mrp_inr = max(product.mrp_inr, price)  # the catalogue requires MRP >= price
    product.price_inr = price
    db.commit()


def test_a_proposal_records_the_task_budget(client: TestClient) -> None:
    h = register(client)
    p = _proposal(client, h)
    assert p["payload"]["budget_max_inr"] == 3_000 and "budget_min_inr" not in p["payload"]


def test_a_price_rise_above_the_task_budget_fails_without_a_replacement(client: TestClient, db: Session) -> None:
    h = register(client)
    p = _proposal(client, h)
    _set_price(db, p["payload"]["product_id"], 3_001)  # one rupee over the ₹3,000 budget
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["details"]["reason"] == "outside_budget" and "new_action_id" not in err["details"]
    assert err["details"]["new_price_inr"] == 3_001 and err["details"]["budget_max_inr"] == 3_000
    assert "₹3,000" in err["message"] and "No new proposal" in err["message"]
    assert [(a["status"], a["result"].get("reason")) for a in fetch(client, h, p["task_id"])["proposals"]] == [
        ("failed", "outside_budget")
    ]
    assert client.get("/api/actions", headers=h, params={"status": "pending"}).json() == []
    assert cart_lines(client, h) == []


def test_a_price_change_within_the_budget_is_reproposed_and_the_budget_carries_over(
    client: TestClient, db: Session
) -> None:
    h = register(client)
    p = _proposal(client, h)
    pid = p["payload"]["product_id"]
    _set_price(db, pid, 3_000)  # exactly the budget: still allowed
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "price_changed"
    fresh = r.json()["error"]["details"]["new_action_id"]
    [replacement] = [a for a in fetch(client, h, p["task_id"])["proposals"] if a["id"] == fresh]
    assert replacement["payload"]["budget_max_inr"] == 3_000 and replacement["payload"]["expected_price_inr"] == 3_000

    _set_price(db, pid, 3_500)  # rises again before the replacement is approved: no further replacement
    r2 = client.post(f"/api/actions/{fresh}/approve", headers=h)
    assert r2.status_code == 409 and r2.json()["error"]["details"]["reason"] == "outside_budget"
    statuses = sorted(a["status"] for a in fetch(client, h, p["task_id"])["proposals"])
    assert statuses == ["failed", "superseded"]  # never a pending proposal above the budget
    assert cart_lines(client, h) == []


def test_a_price_drop_below_a_typed_minimum_fails_too(client: TestClient, db: Session) -> None:
    h = register(client)
    goal = {"goal": "add a wireless mouse between ₹1,000 and ₹3,000 to my cart", "allow_proposals": True}
    [p] = fetch(client, h, start_agent(client, h, goal).json()["id"])["proposals"]
    assert (p["payload"]["budget_min_inr"], p["payload"]["budget_max_inr"]) == (1_000, 3_000)
    _set_price(db, p["payload"]["product_id"], 999)
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "outside_budget"
    assert cart_lines(client, h) == []


# ------------------------------------------------------------------ expiry
def test_an_expired_proposal_reads_as_expired_and_cannot_be_decided(client: TestClient, db: Session) -> None:
    h = register(client)
    p = _proposal(client, h)
    row = db.get(AgentAction, p["id"])
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    # shown as expired everywhere before anyone acts on it, so no Approve button is offered
    assert [a["status"] for a in fetch(client, h, p["task_id"])["proposals"]] == ["expired"]
    assert client.get("/api/actions", headers=h, params={"status": "pending"}).json() == []
    assert [a["id"] for a in client.get("/api/actions", headers=h, params={"status": "expired"}).json()] == [p["id"]]
    for op in ("approve", "reject"):
        r = client.post(f"/api/actions/{p['id']}/{op}", headers=h)
        assert r.status_code == 410 and r.json()["error"]["details"]["status"] == "expired", op
    db.refresh(row)
    assert row.status == "expired" and row.decided_at is not None
    assert cart_lines(client, h) == []


def test_a_proposal_is_approvable_until_it_expires(client: TestClient, db: Session) -> None:
    h = register(client)
    p = _proposal(client, h)
    row = db.get(AgentAction, p["id"])
    assert row is not None
    row.expires_at = datetime.now(UTC) + timedelta(seconds=30)
    db.commit()
    assert client.post(f"/api/actions/{p['id']}/approve", headers=h).status_code == 200
    assert cart_lines(client, h) == [(p["payload"]["product_id"], 1)]


# ------------------------------------------------------------------ never re-proposed above the task budget
@pytest.mark.parametrize(
    ("new_price", "outcome"),
    [
        (1_500, "price_changed"),
        (2_999, "price_changed"),
        (3_000, "price_changed"),
        (3_001, "outside_budget"),
        (9_999, "outside_budget"),
    ],
)
def test_a_task_proposal_is_never_reproposed_above_the_original_budget(
    client: TestClient, db: Session, new_price: int, outcome: str
) -> None:
    """Regression: before the review fix, any price change created a replacement at the new price, even one
    above the ₹3,000 the task was limited to. Now no pending proposal can exist above the task's budget."""
    h = register(client)
    p = _proposal(client, h)
    assert p["payload"]["budget_max_inr"] == 3_000
    _set_price(db, p["payload"]["product_id"], new_price)
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == outcome
    proposals = fetch(client, h, p["task_id"])["proposals"]
    pending = [a for a in proposals if a["status"] == "pending"]
    if outcome == "price_changed":
        assert [a["payload"]["expected_price_inr"] for a in pending] == [new_price]
        assert pending[0]["payload"]["budget_max_inr"] == 3_000 and pending[0]["task_id"] == p["task_id"]
    else:
        assert pending == [] and "new_action_id" not in r.json()["error"]["details"]
    assert all(a["payload"]["expected_price_inr"] <= a["payload"]["budget_max_inr"] for a in pending)
    assert len(client.get("/api/actions", headers=h).json()) == len(proposals)  # nothing created elsewhere
    assert cart_lines(client, h) == []


def test_a_task_without_a_budget_still_reproposes_at_a_new_price(client: TestClient, db: Session) -> None:
    h = register(client)
    [p] = fetch(
        client,
        h,
        start_agent(client, h, {"goal": "add a wireless mouse to my cart", "allow_proposals": True}).json()["id"],
    )["proposals"]
    assert "budget_max_inr" not in p["payload"] and "budget_min_inr" not in p["payload"]
    _set_price(db, p["payload"]["product_id"], p["payload"]["expected_price_inr"] + 5_000)
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "price_changed"


# ------------------------------------------------------------------ the failure path (both databases)
def test_a_failed_approval_records_failure_once_and_leaves_no_partial_cart(client: TestClient, db: Session) -> None:
    h = register(client)
    user_id = uid(client, h)
    p = _proposal(client, h)
    product = db.get(Product, p["payload"]["product_id"])
    assert product is not None
    product.stock = 0
    db.commit()
    r = client.post(f"/api/actions/{p['id']}/approve", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "cart_rejected"
    db.expire_all()
    assert db.scalar(select(Cart).where(Cart.user_id == user_id)) is None  # the cart created mid-way was rolled back
    row = db.get(AgentAction, p["id"])
    assert row is not None and row.status == "failed" and row.result["reason"] == "cart_rejected"
    product.stock = 50
    db.commit()
    again = client.post(f"/api/actions/{p['id']}/approve", headers=h)  # a failed action stays failed
    assert again.status_code == 410 and again.json()["error"]["details"]["status"] == "failed"
    assert cart_lines(client, h) == []


# ------------------------------------------------------------------ action filters: ownership and status
def test_action_filters_return_only_the_callers_actions_by_effective_status(client: TestClient, db: Session) -> None:
    now = datetime.now(UTC)
    owners = {}
    for who in ("me", "other"):
        h = register(client)
        user_id = uid(client, h)
        made = {}
        for label, status, expires in (
            ("live", "pending", now + timedelta(minutes=5)),
            ("lapsed", "pending", now - timedelta(seconds=1)),  # expired, not yet visited
            ("expired", "expired", now - timedelta(minutes=5)),
            ("executed", "executed", now + timedelta(minutes=5)),
            ("rejected", "rejected", now + timedelta(minutes=5)),
        ):
            a = AgentAction(
                id=f"{who}-{label}",
                user_id=user_id,
                kind="add_to_cart",
                payload={"product_id": 1, "quantity": 1, "expected_price_inr": 1},
                summary=label,
                status=status,
                result={},
                expires_at=expires,
            )
            db.add(a)
            made[label] = a.id
        owners[who] = (h, made)
    db.commit()
    h, mine = owners["me"]

    def ids(**params: str) -> set[str]:
        r = client.get("/api/actions", headers=h, params=params)
        assert r.status_code == 200
        return {a["id"] for a in r.json()}

    assert ids(status="pending") == {mine["live"]}
    assert ids(status="expired") == {mine["lapsed"], mine["expired"]}
    assert ids(status="executed") == {mine["executed"]}
    assert ids(status="rejected") == {mine["rejected"]}
    assert ids() == set(mine.values())  # never another user's
    shown = {a["id"]: a["status"] for a in client.get("/api/actions", headers=h).json()}
    assert shown[mine["lapsed"]] == "expired" and shown[mine["live"]] == "pending"
    assert client.get("/api/actions", headers=h, params={"status": "bogus"}).status_code == 422
    assert client.get("/api/actions", params={"status": "pending"}).status_code == 401
    _, theirs = owners["other"]
    for op in ("approve", "reject"):  # and another user's action can't be decided
        assert client.post(f"/api/actions/{theirs['live']}/{op}", headers=h).status_code == 404
    assert db.get(AgentAction, theirs["live"]).status == "pending"  # type: ignore[union-attr]


# ------------------------------------------------------------------ deterministic quantity (S5)
def test_a_planner_cannot_invent_a_quantity(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    from tests.test_agent_loop import SEARCH_MICE, evaluate_found

    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h), ADD_MOUSE)

    def propose_five(state: PlannerState) -> Any:
        return ToolCall(
            tool="propose_add_to_cart", args={"product_id": (state.eligible_ids() or [0])[0], "quantity": 5}
        )

    assert (
        runner(SessionLocal, planner=Scripted(SEARCH_MICE, evaluate_found, propose_five, Final())).run(tid)
        == "succeeded"
    )
    t = fetch(client, h, tid)
    rejected = [s for s in t["steps"] if s["status"] == "skipped"]
    assert [(s["tool"], s["error"]["code"]) for s in rejected] == [("propose_add_to_cart", "invented_quantity")]
    assert actions(SessionLocal) == [] and cart_lines(client, h) == []


def test_the_goals_quantity_is_the_proposals_quantity(client: TestClient) -> None:
    h = register(client)
    goal = {"goal": "add 2 wireless mice under ₹3,000 to my cart", "allow_proposals": True}
    [p] = fetch(client, h, start_agent(client, h, goal).json()["id"])["proposals"]
    assert p["payload"]["quantity"] == 2 and p["summary"].startswith("Add 2 ×")
