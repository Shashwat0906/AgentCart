"""Phase 3 S2: the `evaluate_constraints` read-only tool.

The requirements always come from the task (`ToolContext.constraints`, built from the deterministic
intent). The tool's arguments are only product ids. Decisions reuse `ranking.evaluate`, the function
that filters recommendations, so the tool and the existing filters can never disagree.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.planner import (
    AgentGoal,
    ConstraintReport,
    Final,
    Observation,
    PlannerState,
    RuleBasedPlanner,
    ToolCall,
    check_decision,
    derive_intent,
)
from app.agents.tool_registry import TaskConstraints, ToolContext, build_read_only_registry
from app.core.config import get_settings
from app.models import AgentAction, AgentTask, Base, Cart, CartItem, Order, Product
from app.services.ranking import evaluate, filter_products
from app.services.specs import format_inr
from tests.conftest import register

REGISTRY = build_read_only_registry()


def intent_for(goal: str, **kw: Any):  # type: ignore[no-untyped-def]
    intent, _ = derive_intent(AgentGoal(goal=goal, **kw), max_item_quantity=get_settings().max_item_quantity)
    return intent


def ctx(
    db: Session, goal: str | None = None, *, constraints: TaskConstraints | None = None, user_id: int | None = None
) -> ToolContext:
    if goal is not None:
        constraints = TaskConstraints.from_intent(intent_for(goal))
    return ToolContext(db=db, settings=get_settings(), user_id=user_id, constraints=constraints)


def check(db: Session, goal: str, ids: list[int]) -> dict[int, dict[str, Any]]:
    r = REGISTRY.execute("evaluate_constraints", {"product_ids": ids}, ctx(db, goal))
    assert r.ok, r.error
    assert r.data is not None
    return {res["product_id"]: res for res in r.data["results"]}


def pick(db: Session, pred: Callable[[Product], bool]) -> Product:
    p = next((p for p in db.scalars(select(Product).order_by(Product.id)) if pred(p)), None)
    assert p is not None, "no product in the seeded catalogue matches"
    return p


def ram(p: Product) -> int:
    return int((p.specs or {}).get("ram_gb") or 0)


# ------------------------------------------------------------------ input validation
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"product_ids": []},
        {"product_ids": list(range(1, 26))},  # more than 24
        {"product_ids": [1, 1]},
        {"product_ids": [0]},
        {"product_ids": [-3]},
        {"product_ids": ["1"]},  # strict: no coercion
        {"product_ids": [1.0]},
        {"product_ids": [True]},
        {"product_ids": "1,2"},
        # Requirements can never be supplied, loosened or replaced through arguments.
        {"product_ids": [1], "constraints": []},
        {"product_ids": [1], "max_price_inr": 10_000_000},
        {"product_ids": [1], "category": "mice"},
        {"product_ids": [1], "ignore_constraints": True},
    ],
)
def test_invalid_arguments_are_rejected(db: Session, args: dict[str, Any]) -> None:
    r = REGISTRY.execute("evaluate_constraints", args, ctx(db, "laptops under ₹50,000"))
    assert not r.ok and r.error is not None and r.error.code == "invalid_input", args


def test_non_object_arguments_are_rejected(db: Session) -> None:
    r = REGISTRY.execute("evaluate_constraints", [1, 2], ctx(db, "laptops under ₹50,000"))  # type: ignore[arg-type]
    assert r.error is not None and r.error.code == "invalid_input"


def test_the_task_requirements_are_required(db: Session) -> None:
    r = REGISTRY.execute("evaluate_constraints", {"product_ids": [1]}, ctx(db, constraints=None))
    assert r.error is not None and r.error.code == "invalid_request"


def test_unknown_products_are_not_found(db: Session) -> None:
    r = REGISTRY.execute("evaluate_constraints", {"product_ids": [1, 99_999]}, ctx(db, "laptops under ₹50,000"))
    assert r.error is not None and r.error.code == "not_found"


def test_schema_exposes_only_product_ids() -> None:
    spec = REGISTRY.get("evaluate_constraints")
    assert spec is not None and spec.permission == "read" and spec.requires_user is False
    schema = spec.schema()["input_schema"]
    assert set(schema["properties"]) == {"product_ids"} and schema["additionalProperties"] is False
    assert schema["properties"]["product_ids"]["maxItems"] == 24


# ------------------------------------------------------------------ budget, category, specs, stock
def test_budget(db: Session) -> None:
    laptops = list(
        db.scalars(select(Product).where(Product.category == "laptops", Product.stock > 0).order_by(Product.id))
    )
    report = check(db, "laptops under ₹50,000", [p.id for p in laptops[:24]])
    for p in laptops[:24]:
        res = report[p.id]
        assert res["passes"] is (p.price_inr <= 50_000), (p.name, p.price_inr)
        if p.price_inr > 50_000:
            assert res["violations"] == [f"{format_inr(p.price_inr - 50_000)} over your budget"]
        else:
            assert res["violations"] == []


def test_exact_budget_boundary_passes(db: Session) -> None:
    p = pick(db, lambda p: p.category == "laptops" and p.stock > 0)
    assert check(db, f"laptops under ₹{p.price_inr}", [p.id])[p.id]["passes"] is True
    over = check(db, f"laptops under ₹{p.price_inr - 1}", [p.id])[p.id]
    assert over["passes"] is False and over["violations"] == ["₹1 over your budget"]


def test_category(db: Session) -> None:
    mouse = pick(db, lambda p: p.category == "mice" and p.stock > 0 and p.price_inr <= 50_000)
    res = check(db, "laptops under ₹50,000", [mouse.id])[mouse.id]
    assert res["passes"] is False and res["violations"] == ["Category: Mice (you asked for Laptops)"]


def test_ram_minimum(db: Session) -> None:
    small = pick(db, lambda p: p.category == "laptops" and p.stock > 0 and p.price_inr <= 70_000 and ram(p) < 16)
    big = pick(db, lambda p: p.category == "laptops" and p.stock > 0 and p.price_inr <= 70_000 and ram(p) >= 16)
    report = check(db, "laptops under ₹70,000 with at least 16GB RAM", [small.id, big.id])
    assert report[small.id] == {
        "product_id": small.id,
        "passes": False,
        "violations": [f"RAM: {ram(small)} GB (you asked RAM ≥ 16 GB)"],
    }
    assert report[big.id] == {"product_id": big.id, "passes": True, "violations": []}


def test_boolean_spec(db: Session) -> None:
    wired = pick(db, lambda p: p.category == "mice" and p.stock > 0 and (p.specs or {}).get("wireless") is False)
    wireless = pick(db, lambda p: p.category == "mice" and p.stock > 0 and (p.specs or {}).get("wireless") is True)
    report = check(db, "a wireless mouse", [wired.id, wireless.id])
    assert report[wired.id]["passes"] is False and len(report[wired.id]["violations"]) == 1
    assert "Wireless" in report[wired.id]["violations"][0]
    assert report[wireless.id]["passes"] is True


def test_out_of_stock_fails_even_when_every_constraint_holds(db: Session) -> None:
    gone = pick(db, lambda p: p.category == "mice" and p.stock == 0)
    res = check(db, f"mice under ₹{gone.price_inr + 1000}", [gone.id])[gone.id]
    assert res == {"product_id": gone.id, "passes": False, "violations": ["Out of stock"]}


def test_a_product_failing_several_constraints_lists_every_violation(db: Session) -> None:
    over_and_small = pick(
        db, lambda p: p.category == "laptops" and p.stock > 0 and p.price_inr > 30_000 and ram(p) < 16
    )
    res = check(db, "laptops under ₹30,000 with at least 16GB RAM", [over_and_small.id])[over_and_small.id]
    assert res["passes"] is False and len(res["violations"]) == 2
    assert any("over your budget" in v for v in res["violations"]) and any(
        v.startswith("RAM:") for v in res["violations"]
    )

    mouse = pick(db, lambda p: p.category == "mice" and p.stock > 0 and p.price_inr > 500)
    res = check(db, "laptops under ₹500 with at least 16GB RAM", [mouse.id])[mouse.id]
    assert res["violations"][0] == "Category: Mice (you asked for Laptops)"
    assert any("over your budget" in v for v in res["violations"])
    # A mouse has no RAM: an unknown attribute counts as a failure (existing `_violation` wording).
    assert any(v.endswith("unknown (you required RAM ≥ 16 GB)") for v in res["violations"])
    assert len(res["violations"]) == 3

    gone = pick(db, lambda p: p.category == "mice" and p.stock == 0)
    res = check(db, "laptops under ₹1,000", [gone.id])[gone.id]
    assert {"Out of stock"} < set(res["violations"]) and len(res["violations"]) >= 3


def test_results_follow_the_requested_order_and_name_the_requirements(db: Session) -> None:
    ids = [30, 1, 12, 7]
    r = REGISTRY.execute(
        "evaluate_constraints", {"product_ids": ids}, ctx(db, "laptops under ₹50,000 with at least 16GB RAM")
    )
    assert r.ok and r.data is not None
    assert [res["product_id"] for res in r.data["results"]] == ids
    assert r.data["constraints"] == ["Category: Laptops", "RAM ≥ 16 GB", "Price ≤ ₹50,000", "In stock"]
    ConstraintReport.model_validate(r.data)  # matches the planner's contract


# ------------------------------------------------------------------ same answer as the existing filters
@pytest.mark.parametrize(
    "goal",
    [
        "laptops under ₹50,000",
        "laptops under ₹70,000 with at least 16GB RAM",
        "Find laptops strictly under ₹50,000 with at least 16GB RAM",
        "a wireless mouse under ₹3,000",
        "noise cancelling headphones under ₹10,000",
        "Samsung phone under 30000",
        "phone under ₹20,000 with at least 5000 mAh battery",
        "monitor between 15000 and 30000 with 144Hz or more",
        "mechanical keyboard",
    ],
)
def test_agrees_with_filter_products_across_the_whole_catalogue(db: Session, goal: str) -> None:
    intent = intent_for(goal)
    products = list(db.scalars(select(Product).order_by(Product.id)))
    expected = {p.id for p in filter_products(products, intent).eligible}
    passed: set[int] = set()
    for i in range(0, len(products), 24):
        chunk = [p.id for p in products[i : i + 24]]
        passed |= {pid for pid, res in check(db, goal, chunk).items() if res["passes"]}
    assert passed == expected, goal
    for p in products:  # and every passing product really satisfies every hard constraint
        if p.id in passed:
            assert p.stock > 0 and p.category == intent.category
            assert all(evaluate(c, p) is True for c in intent.hard_constraints())


# ------------------------------------------------------------------ read-only and planner integration
def test_never_writes(db: Session, client: TestClient) -> None:
    def counts() -> dict[str, int]:
        return {t.name: int(db.scalar(select(func.count()).select_from(t)) or 0) for t in Base.metadata.sorted_tables}

    register(client)
    before = counts()
    prices = list(db.scalars(select(Product.price_inr).order_by(Product.id)))
    for goal in ("laptops under ₹50,000 with at least 16GB RAM", "a wireless mouse under ₹3,000"):
        check(db, goal, list(range(1, 25)))
    db.commit()  # anything left pending by the tool would be persisted here
    assert counts() == before
    assert list(db.scalars(select(Product.price_inr).order_by(Product.id))) == prices
    for model in (Cart, CartItem, Order, AgentAction, AgentTask):
        assert int(db.scalar(select(func.count()).select_from(model)) or 0) == before[model.__tablename__]


@pytest.mark.parametrize(
    "goal",
    [
        "a wireless mouse under ₹3,000, add the cheapest to my cart",
        "Find laptops strictly under ₹50,000 with at least 16GB RAM",
        "laptops under ₹70,000 with at least 16GB RAM",
        "noise cancelling headphones",
    ],
)
def test_rule_based_planner_with_the_real_tool(db: Session, client: TestClient, goal: str) -> None:
    uid = int(client.get("/api/auth/me", headers=register(client)).json()["id"])
    g = AgentGoal(goal=goal)
    intent, questions = derive_intent(g, max_item_quantity=get_settings().max_item_quantity)
    state = PlannerState(goal=g, intent=intent, questions=questions)
    tools = REGISTRY.schemas()  # the real registry: S1's stand-in for this tool is no longer needed
    c = ToolContext(db=db, settings=get_settings(), user_id=uid, constraints=TaskConstraints.from_intent(intent))
    planner = RuleBasedPlanner()
    for _ in range(8):
        d = planner.decide(state, tools)
        assert check_decision(d, state, tools) is None
        if not isinstance(d, ToolCall):
            break
        r = REGISTRY.execute(d.tool, d.args, c)
        assert r.ok, (d.tool, r.error)
        state = PlannerState(
            goal=g,
            intent=intent,
            observations=(*state.observations, Observation(tool=d.tool, args=d.args, ok=True, data=r.data)),
        )
    assert isinstance(d, Final)
    for pid in d.product_ids:
        p = db.get(Product, pid)
        assert p is not None and p.stock > 0 and p.category == intent.category
        assert all(evaluate(con, p) is True for con in intent.hard_constraints())
    if goal.startswith("Find laptops strictly"):
        assert d.product_ids == []  # nothing ≤ ₹50,000 has 16 GB of RAM
