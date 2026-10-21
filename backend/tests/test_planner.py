"""Phase 3 S1: planner contracts and the deterministic rule-based planner (not yet wired into the runner).

A small harness plays the role of the future loop runner: it executes each ToolCall with the real
read-only registry tools against the seeded test database. `evaluate_constraints` (S2) is computed as
ground truth with the same `ranking.evaluate` the app uses, and proposals (S4) are only recorded.
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.agents.planner import (
    AgentGoal,
    Clarify,
    ConstraintReport,
    Final,
    InvalidDecision,
    Observation,
    PlannerState,
    RuleBasedPlanner,
    ToolCall,
    check_decision,
    derive_intent,
    parse_decision,
)
from app.agents.tool_registry import ToolContext, build_read_only_registry
from app.core.config import get_settings
from app.models import Product
from app.services import catalog
from app.services.ranking import evaluate
from tests.conftest import register

REGISTRY = build_read_only_registry()
ALL_TOOLS: list[dict[str, Any]] = [
    *REGISTRY.schemas(),
    {"name": "evaluate_constraints", "permission": "read", "requires_user": False, "input_schema": {}},
    {"name": "propose_add_to_cart", "permission": "propose", "requires_user": True, "input_schema": {}},
]


def tools_without(*names: str) -> list[dict[str, Any]]:
    return [t for t in ALL_TOOLS if t["name"] not in names]


def state_for(goal: str, **kw: Any) -> PlannerState:
    g = AgentGoal(goal=goal, **kw)
    intent, questions = derive_intent(g, max_item_quantity=get_settings().max_item_quantity)
    return PlannerState(goal=g, intent=intent, questions=questions)


def drive(
    db: Session,
    state: PlannerState,
    tools: list[dict[str, Any]] | None = None,
    user_id: int | None = None,
    fail: Callable[[ToolCall], str | None] | None = None,
    max_iterations: int = 10,
) -> tuple[list[ToolCall | Final | Clarify], PlannerState]:
    """Run the planner to completion. Every decision must pass `check_decision`."""
    tools = ALL_TOOLS if tools is None else tools
    planner = RuleBasedPlanner()
    decisions: list[ToolCall | Final | Clarify] = []
    for _ in range(max_iterations):
        d = planner.decide(state, tools)
        assert check_decision(d, state, tools) is None, (d, check_decision(d, state, tools))
        decisions.append(d)
        if not isinstance(d, ToolCall):
            return decisions, state
        failure = fail(d) if fail else None
        if failure:
            obs = Observation(tool=d.tool, args=d.args, ok=False, error_code=failure)
        elif d.tool == "evaluate_constraints":
            obs = Observation(
                tool=d.tool, args=d.args, ok=True, data=_ground_truth_report(db, state, d.args["product_ids"])
            )
        elif d.tool == "propose_add_to_cart":
            obs = Observation(tool=d.tool, args=d.args, ok=True, data={"action_id": "simulated", "status": "pending"})
        else:
            r = REGISTRY.execute(d.tool, d.args, ToolContext(db=db, settings=get_settings(), user_id=user_id))
            assert r.ok, (d.tool, r.error)  # every planned call is valid for the real tool schema
            obs = Observation(tool=d.tool, args=d.args, ok=True, data=r.data)
        state = PlannerState(
            goal=state.goal, intent=state.intent, questions=state.questions, observations=(*state.observations, obs)
        )
    raise AssertionError(f"planner did not finish within {max_iterations} decisions")


def _ground_truth_report(db: Session, state: PlannerState, ids: list[int]) -> dict[str, Any]:
    hard = state.intent.hard_constraints()
    by_id = {p.id: p for p in catalog.products_by_ids(db, ids)}
    results = []
    for pid in ids:
        failed = [c.label or c.attribute for c in hard if evaluate(c, by_id[pid]) is not True]
        results.append({"product_id": pid, "passes": not failed, "violations": failed})
    return ConstraintReport.model_validate({"results": results}).model_dump()


def tools_used(decisions: list[ToolCall | Final | Clarify]) -> list[str]:
    return [d.tool if isinstance(d, ToolCall) else d.kind for d in decisions]


def satisfies_all(db: Session, state: PlannerState, pid: int) -> bool:
    p = db.get(Product, pid)
    assert p is not None
    return p.stock > 0 and all(evaluate(c, p) is True for c in state.intent.hard_constraints())


@pytest.fixture()
def user_id(client: TestClient) -> int:
    return int(client.get("/api/auth/me", headers=register(client)).json()["id"])


# ------------------------------------------------------------------ decision schemas
@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "tool", "tool": "search_catalog", "args": {"category": "laptops"}, "rationale": "find laptops"},
        {"kind": "final", "product_ids": [1, 2], "notes": "done"},
        {"kind": "clarify", "question": "Which category?"},
    ],
)
def test_valid_decisions_parse(raw: dict[str, Any]) -> None:
    assert parse_decision(raw).kind == raw["kind"]


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "execute_sql", "sql": "DROP TABLE products"},
        {"kind": "tool", "tool": "search_catalog", "args": {}, "code": "import os"},  # extra field
        {"kind": "tool", "tool": "Search Catalog", "args": {}},  # not a registry name
        {"kind": "tool", "tool": "search_catalog", "args": "category=laptops"},
        {"kind": "tool", "tool": "search_catalog", "rationale": "x" * 301},
        {"kind": "tool"},
        {"kind": "final", "product_ids": ["1"]},  # strict: no coercion
        {"kind": "final", "product_ids": [1, 1]},
        {"kind": "final", "product_ids": [0]},
        {"kind": "final", "product_ids": [1, 2, 3, 4, 5]},
        {"kind": "final", "notes": "x" * 501},
        {"kind": "clarify", "question": ""},
        {"tool": "search_catalog"},  # no kind
        "search_catalog",
        ["tool"],
        None,
    ],
)
def test_malformed_decisions_are_rejected_without_echoing_input(raw: Any) -> None:
    with pytest.raises(InvalidDecision) as exc:
        parse_decision(raw)
    assert exc.value.code == "invalid_decision" and exc.value.errors
    assert "DROP TABLE" not in repr(exc.value.errors) and "import os" not in repr(exc.value.errors)


def test_decisions_are_immutable() -> None:
    d = parse_decision({"kind": "tool", "tool": "read_cart"})
    assert isinstance(d, ToolCall)
    with pytest.raises(ValidationError):  # frozen: a decision can't be changed after validation
        d.tool = "propose_add_to_cart"  # type: ignore[misc]


# ------------------------------------------------------------------ goal input and deterministic intent
@pytest.mark.parametrize(
    "raw",
    [
        {"goal": "hi"},
        {"goal": "x" * 501},
        {"goal": "laptops", "category": "cars"},
        {"goal": "laptops", "budget_max_inr": "50000"},
        {"goal": "laptops", "budget_max_inr": 0},
        {"goal": "laptops", "allow_proposals": "yes"},
        {"goal": "laptops", "max_iterations": 99},
    ],
)
def test_agent_goal_is_strict(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AgentGoal.model_validate(raw)


def test_intent_comes_from_the_text_and_can_only_be_tightened() -> None:
    s = state_for("Find laptops strictly under ₹50,000 with at least 16GB RAM")
    assert (s.intent.category, s.intent.budget_max) == ("laptops", 50_000)
    assert {c.label for c in s.intent.hard_constraints()} == {"RAM ≥ 16 GB", "Price ≤ ₹50,000"}
    assert state_for("laptops under ₹50,000", budget_max_inr=40_000).intent.budget_max == 40_000  # tighter wins
    assert state_for("laptops under ₹50,000", budget_max_inr=90_000).intent.budget_max == 50_000  # can't loosen
    assert state_for("something for gaming", category="mice").intent.category == "mice"
    assert state_for("laptops‮ under ₹50,000\x00").intent.budget_max == 50_000  # sanitised like chat input


# ------------------------------------------------------------------ rule-based plans on real catalogue data
def test_full_plan_with_an_approved_proposal(db: Session, user_id: int) -> None:
    s = state_for("a wireless mouse under ₹3,000, add the cheapest to my cart", allow_proposals=True)
    decisions, end = drive(db, s, user_id=user_id)
    assert tools_used(decisions) == [
        "search_catalog",
        "evaluate_constraints",
        "compare_products",
        "read_cart",
        "propose_add_to_cart",
        "final",
    ]
    propose = decisions[4]
    assert isinstance(propose, ToolCall)
    proposed = db.get(Product, propose.args["product_id"])
    assert (
        proposed is not None and proposed.name == "HP X200 Wireless Mouse" and proposed.price_inr == 599
    )  # the cheapest eligible
    assert propose.args["quantity"] == 1
    final = decisions[-1]
    assert isinstance(final, Final) and final.product_ids
    assert all(satisfies_all(db, end, pid) for pid in final.product_ids)


def test_no_proposal_unless_the_task_allows_it(db: Session, user_id: int) -> None:
    decisions, _ = drive(db, state_for("a wireless mouse under ₹3,000, add the cheapest to my cart"), user_id=user_id)
    assert "propose_add_to_cart" not in tools_used(decisions)
    final = decisions[-1]
    assert isinstance(final, Final) and "weren't enabled" in final.notes


def test_no_proposal_when_the_user_did_not_ask(db: Session, user_id: int) -> None:
    decisions, _ = drive(
        db, state_for("noise cancelling headphones under ₹10,000", allow_proposals=True), user_id=user_id
    )
    assert "propose_add_to_cart" not in tools_used(decisions)


def test_hard_constraints_with_no_eligible_product_end_with_nothing(db: Session, user_id: int) -> None:
    # The production bug scenario: nothing at or under ₹50,000 has 16 GB of RAM.
    s = state_for("Find laptops strictly under ₹50,000 with at least 16GB RAM, add the best one", allow_proposals=True)
    decisions, _ = drive(db, s, user_id=user_id)
    assert tools_used(decisions) == ["search_catalog", "evaluate_constraints", "final"]
    final = decisions[-1]
    assert isinstance(final, Final) and final.product_ids == [] and "None of the" in final.notes


def test_a_single_eligible_product_skips_the_comparison(db: Session, user_id: int) -> None:
    decisions, end = drive(db, state_for("laptops under ₹30,000"), user_id=user_id)
    assert "compare_products" not in tools_used(decisions)
    final = decisions[-1]
    assert isinstance(final, Final) and len(final.product_ids) == 1 and satisfies_all(db, end, final.product_ids[0])


def test_no_search_results(db: Session) -> None:
    decisions, _ = drive(db, state_for("laptops under ₹500"), tools=tools_without("read_cart"))
    assert tools_used(decisions) == ["search_catalog", "final"]


@pytest.mark.parametrize("goal", ["something nice", "help me out"])
def test_vague_goals_ask_before_calling_any_tool(db: Session, goal: str) -> None:
    decisions, _ = drive(db, state_for(goal))
    assert len(decisions) == 1 and isinstance(decisions[0], Clarify)


def test_best_rated_order_by_default_and_cheapest_on_request(db: Session) -> None:
    cheap, _ = drive(db, state_for("wireless mouse under ₹10,000, the cheapest"), tools=tools_without("read_cart"))
    rated, _ = drive(db, state_for("wireless mouse under ₹10,000"), tools=tools_without("read_cart"))
    cheap_final, rated_final = cheap[-1], rated[-1]
    assert isinstance(cheap_final, Final) and isinstance(rated_final, Final)
    prices = [db.get(Product, i).price_inr for i in cheap_final.product_ids]  # type: ignore[union-attr]
    ratings = [db.get(Product, i).rating for i in rated_final.product_ids]  # type: ignore[union-attr]
    assert prices == sorted(prices) and ratings == sorted(ratings, reverse=True)


# ------------------------------------------------------------------ only offered tools; failures; determinism
@pytest.mark.parametrize("missing", ["compare_products", "read_cart", "propose_add_to_cart"])
def test_the_planner_only_uses_tools_it_is_offered(db: Session, user_id: int, missing: str) -> None:
    tools = tools_without(missing)
    s = state_for("a wireless mouse under ₹3,000, add the cheapest to my cart", allow_proposals=True)
    decisions, _ = drive(db, s, tools=tools, user_id=user_id)
    assert missing not in tools_used(decisions)
    assert isinstance(decisions[-1], Final)


def test_without_a_constraint_check_nothing_is_recommended(db: Session) -> None:
    decisions, _ = drive(db, state_for("wireless mouse under ₹3,000"), tools=tools_without("evaluate_constraints"))
    final = decisions[-1]
    assert tools_used(decisions) == ["search_catalog", "final"]
    assert isinstance(final, Final) and final.product_ids == [] and "can't be verified" in final.notes


def test_a_failed_step_stops_the_plan_instead_of_looping(db: Session) -> None:
    decisions, _ = drive(
        db, state_for("wireless mouse under ₹3,000"), fail=lambda d: "not_found" if d.tool == "search_catalog" else None
    )
    assert tools_used(decisions) == ["search_catalog", "final"]
    final = decisions[-1]
    assert isinstance(final, Final) and final.product_ids == [] and "not_found" in final.notes


def test_decisions_are_deterministic(db: Session, user_id: int) -> None:
    s = state_for("a wireless mouse under ₹3,000, add the cheapest to my cart", allow_proposals=True)
    a, _ = drive(db, s, user_id=user_id)
    b, _ = drive(db, s, user_id=user_id)
    assert [d.model_dump() for d in a] == [d.model_dump() for d in b]
    assert RuleBasedPlanner().decide(s, ALL_TOOLS) == RuleBasedPlanner().decide(s, ALL_TOOLS)


@pytest.mark.parametrize(
    "goal",
    [
        "a wireless mouse under ₹3,000, add the cheapest to my cart",
        "Find laptops strictly under ₹50,000 with at least 16GB RAM",
        "noise cancelling headphones",
        "Samsung phone under 30000",
        "monitor between 15000 and 30000 with 144Hz or more",
    ],
)
def test_every_recommendation_meets_every_hard_constraint(db: Session, user_id: int, goal: str) -> None:
    decisions, end = drive(db, state_for(goal, allow_proposals=True), user_id=user_id)
    final = decisions[-1]
    assert isinstance(final, Final)
    assert all(satisfies_all(db, end, pid) for pid in final.product_ids)
    assert len(decisions) <= 6  # bounded without any external limit


# ------------------------------------------------------------------ grounding guard
def _evaluated(state: PlannerState, passing: list[int], failing: list[int]) -> PlannerState:
    report = {
        "results": [{"product_id": i, "passes": True, "violations": []} for i in passing]
        + [{"product_id": i, "passes": False, "violations": ["RAM ≥ 16 GB"]} for i in failing]
    }
    obs = Observation(tool="evaluate_constraints", args={}, ok=True, data=report)
    return PlannerState(goal=state.goal, intent=state.intent, observations=(obs,))


def test_check_decision_rejects_unoffered_tools_and_ungrounded_products() -> None:
    s = _evaluated(state_for("wireless mouse under ₹3,000", allow_proposals=True), passing=[5, 6], failing=[7])
    assert check_decision(ToolCall(tool="checkout_now"), s, ALL_TOOLS) == "unknown_tool"
    assert (
        check_decision(ToolCall(tool="propose_add_to_cart", args={"product_id": 7, "quantity": 1}), s, ALL_TOOLS)
        == "ungrounded_proposal"
    )
    assert (
        check_decision(ToolCall(tool="propose_add_to_cart", args={"product_id": 99, "quantity": 1}), s, ALL_TOOLS)
        == "ungrounded_proposal"
    )
    assert check_decision(Final(product_ids=[5, 7]), s, ALL_TOOLS) == "ungrounded_final"
    assert check_decision(Final(product_ids=[5, 6]), s, ALL_TOOLS) is None
    assert (
        check_decision(ToolCall(tool="propose_add_to_cart", args={"product_id": 5, "quantity": 1}), s, ALL_TOOLS)
        is None
    )
    unchecked = state_for("wireless mouse under ₹3,000")
    assert check_decision(Final(product_ids=[5]), unchecked, ALL_TOOLS) == "ungrounded_final"  # nothing was checked
    assert check_decision(Final(), unchecked, ALL_TOOLS) is None


# ------------------------------------------------------------------ offline: no provider, no network
def test_planner_module_needs_no_provider_or_network() -> None:
    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "agents" / "planner.py").read_text(encoding="utf-8")
    for forbidden in ("app.providers", "httpx", "requests", "urllib", "socket", "os.environ", "sqlalchemy", "Session"):
        assert forbidden not in src, forbidden


def test_rule_based_planner_runs_with_no_api_keys(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    for var in ("ANTHROPIC_API_KEY", "HOSTED_LLM_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    decisions, _ = drive(db, state_for("wireless mouse under ₹3,000"), tools=tools_without("read_cart"))
    assert isinstance(decisions[-1], Final)


def test_check_decision_rejects_quantities_the_user_did_not_ask_for() -> None:
    """S5: the quantity comes from the goal, deterministically; no planner (or model) may choose another."""
    one = _evaluated(
        state_for("add a wireless mouse under ₹3,000 to my cart", allow_proposals=True), passing=[5], failing=[]
    )
    assert one.intent.cart_quantity == 1

    def propose(q: int | None) -> ToolCall:
        return ToolCall(
            tool="propose_add_to_cart", args={"product_id": 5, **({"quantity": q} if q is not None else {})}
        )

    assert check_decision(propose(1), one, ALL_TOOLS) is None
    assert check_decision(propose(None), one, ALL_TOOLS) is None  # the tool's default is 1
    for q in (0, 2, 5, 10, 100):
        assert check_decision(propose(q), one, ALL_TOOLS) == "invented_quantity", q
    two = _evaluated(
        state_for("add 2 wireless mice under ₹3,000 to my cart", allow_proposals=True), passing=[5], failing=[]
    )
    assert two.intent.cart_quantity == 2
    assert check_decision(propose(2), two, ALL_TOOLS) is None
    assert check_decision(propose(1), two, ALL_TOOLS) == "invented_quantity"
    assert check_decision(propose(None), two, ALL_TOOLS) == "invented_quantity"
