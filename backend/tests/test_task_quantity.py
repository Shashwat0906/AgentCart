"""Agent tasks and the per-product quantity limit (MAX_ITEM_QUANTITY, 10 by default).

Regression: "add 20 wireless mice under ₹3,000 to my cart" used to run the whole research, then fail at the
proposal step ("You can buy at most 10 units of one product"), losing the research. The limit was only a
non-blocking note. It is now a clarifying question asked before any tool runs; nothing is capped silently.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.agents.parser import quantity_mentions
from app.agents.planner import AgentGoal, _cart_quantity, derive_intent
from app.core.config import get_settings
from app.models import AgentAction, CartItem
from app.services.tasks import TaskRunner
from tests.conftest import register
from tests.test_agent_loop import agent_task, start_agent
from tests.test_tasks import Calls, counts, fetch, uid

TWENTY = "add 20 wireless mice under ₹3,000 to my cart"


def questions(goal: str, *, allow: bool = True, limit: int = 10) -> list[dict[str, Any]]:
    return derive_intent(AgentGoal(goal=goal, allow_proposals=allow), max_item_quantity=limit)[1]


@pytest.mark.parametrize(
    ("goal", "allow", "limit", "asks"),
    [
        (TWENTY, True, 10, True),
        ("add 11 wireless mice under ₹3,000 to my cart", True, 10, True),
        ("add 10 wireless mice under ₹3,000 to my cart", True, 10, False),  # exactly the limit is fine
        ("add 2 wireless mice under ₹3,000 to my cart", True, 10, False),
        ("add a wireless mouse under ₹3,000 to my cart", True, 10, False),
        ("add 4 wireless mice under ₹3,000 to my cart", True, 3, True),  # the configured limit applies
        (TWENTY, False, 10, False),  # no proposals: the quantity can't matter, research runs normally
        ("20 wireless mice under ₹3,000", True, 10, False),  # not asked to add to the cart
    ],
)
def test_an_over_limit_cart_quantity_is_a_blocking_question(goal: str, allow: bool, limit: int, asks: bool) -> None:
    qs = [q for q in questions(goal, allow=allow, limit=limit) if q["id"] == "quantity"]
    assert bool(qs) is asks
    if asks:
        assert f"at most {limit} units" in qs[0]["question"] and f"(1–{limit})" in qs[0]["question"]


def test_the_quantity_question_leaves_every_other_requirement_untouched() -> None:
    intent, qs = derive_intent(AgentGoal(goal=TWENTY, allow_proposals=True), max_item_quantity=10)
    baseline, _ = derive_intent(AgentGoal(goal=TWENTY, allow_proposals=False), max_item_quantity=10)
    assert intent == baseline  # nothing capped or rewritten: category, budget, constraints, quantity as parsed
    assert (intent.category, intent.budget_max, intent.cart_quantity) == ("mice", 3_000, 20)
    assert [q["id"] for q in qs] == ["quantity"]


def test_an_over_limit_request_is_clarified_before_any_research(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h), {"goal": TWENTY, "allow_proposals": True})
    calls = Calls()
    runner = TaskRunner(SessionLocal, get_settings(), registry_factory=calls.registry, sleep=lambda _s: None)
    assert runner.run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert [s["tool"] for s in t["steps"]] == ["clarify"] and sum(calls.count.values()) == 0  # no tool ran
    assert t["result"]["type"] == "clarification" and "at most 10 units" in t["result"]["question"]
    assert t["state"]["tool_calls"] == 0 and t["error"] is None and t["proposals"] == []
    with SessionLocal() as s:
        assert counts(s, AgentAction, CartItem) == {"agent_actions": 0, "cart_items": 0}


def test_without_proposals_the_same_request_still_researches(client: TestClient) -> None:
    h = register(client)
    t = fetch(client, h, start_agent(client, h, {"goal": TWENTY}).json()["id"])
    assert t["status"] == "succeeded" and t["result"]["type"] == "recommendations"
    assert "propose_add_to_cart" not in {s["tool"] for s in t["steps"]} and t["proposals"] == []
    assert all(p["category"] == "mice" and p["price_inr"] <= 3_000 for p in t["result"]["products"])


def test_exactly_the_limit_is_proposed_within_every_hard_constraint(client: TestClient) -> None:
    h = register(client)
    t = fetch(
        client,
        h,
        start_agent(
            client, h, {"goal": "add 10 wireless mice under ₹3,000 to my cart", "allow_proposals": True}
        ).json()["id"],
    )
    assert t["status"] == "succeeded"
    [p] = t["proposals"]
    assert p["status"] == "pending" and p["payload"]["quantity"] == 10
    assert p["payload"]["expected_price_inr"] <= 3_000 and p["payload"]["budget_max_inr"] == 3_000
    product = client.get(f"/api/products/{p['payload']['product_id']}").json()
    assert product["category"] == "mice" and product["stock"] >= 10  # stock covers the quantity
    assert client.get("/api/cart", headers=h).json()["item_count"] == 0  # proposed, not added


def test_the_propose_tool_still_refuses_over_limit_quantities(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    """Defence in depth: even if a request reached the proposal step, the tool's own limit still holds."""
    from app.agents.planner import Final, ToolCall
    from tests.test_agent_loop import SEARCH_MICE, Scripted, evaluate_found

    h = register(client)
    tid = agent_task(
        SessionLocal, uid(client, h), {"goal": "add a wireless mouse under ₹3,000 to my cart", "allow_proposals": True}
    )

    def propose_twenty(state: Any) -> Any:
        return ToolCall(
            tool="propose_add_to_cart", args={"product_id": (state.eligible_ids() or [0])[0], "quantity": 20}
        )

    runner = TaskRunner(
        SessionLocal,
        get_settings(),
        sleep=lambda _s: None,
        planner_factory=lambda: Scripted(SEARCH_MICE, evaluate_found, propose_twenty, Final()),
    )
    runner.run(tid)
    t = fetch(client, h, tid)
    assert t["proposals"] == [] and client.get("/api/cart", headers=h).json()["item_count"] == 0


# ------------------------------------------------------------------ quantity belongs to the product it names
@pytest.mark.parametrize(
    ("text", "mentions"),
    [
        ("add 20 wireless mice under ₹3,000 to my cart", [(20, "mice")]),
        ("add 2 mice and 3 keyboards to my cart", [(2, "mice"), (3, "keyboards")]),
        ("a wireless mouse under ₹3,000. add 10 of the most expensive laptop to my cart", [(10, "laptops")]),
        ("add a wireless mouse under ₹3,000 and 2 keyboards to my cart", [(2, "keyboards")]),
        ("add two mice to my cart", [(2, "mice")]),
        ("add 3 units of the keyboard to my cart", [(3, "keyboards")]),
        ("add 2 to my cart", [(2, None)]),
        # not quantities: prices, specs, counts of something else
        ("add a wireless mouse under ₹3,000 to my cart", []),
        ("add a 16gb laptop under ₹60,000 to my cart", []),
        ("get 4k monitor and add it to my cart", []),
        ("add a wireless mouse for my 2 kids to my cart", []),
        ("add a wireless mouse with 3 buttons to my cart", []),
        ("add a laptop with 2 years warranty under ₹60,000 to my cart", []),
    ],
)
def test_quantity_mentions_are_tied_to_the_product_they_name(text: str, mentions: list[tuple[int, str | None]]) -> None:
    assert quantity_mentions(text) == mentions


@pytest.mark.parametrize(
    ("goal", "qid", "quantity"),
    [
        ("add 2 mice and 3 keyboards to my cart", "quantity_ambiguous", None),
        ("add 2 wireless mice and 2 keyboards to my cart", "quantity_ambiguous", None),
        ("a wireless mouse under ₹3,000. add 10 of the most expensive laptop to my cart", "quantity_product", None),
        ("add a wireless mouse under ₹3,000 and 2 keyboards to my cart", "quantity_product", None),
        ("add 2 wireless mice under ₹3,000 to my cart", None, 2),
        ("add two mice to my cart", None, 2),
        ("add 2 to my cart, a wireless mouse under ₹3,000", None, 2),  # no product named: the task's product
        ("add a wireless mouse for my 2 kids to my cart", None, 1),
    ],
)
def test_a_quantity_is_never_moved_to_another_product(goal: str, qid: str | None, quantity: int | None) -> None:
    intent, qs = derive_intent(AgentGoal(goal=goal, allow_proposals=True), max_item_quantity=10)
    assert intent.category == "mice"
    if qid is None:
        assert qs == [] and intent.cart_quantity == quantity
    else:
        assert [q["id"] for q in qs] == [qid] and "(1–10)" in qs[0]["question"] and "mice" in qs[0]["question"]


def test_a_quantity_the_parser_read_but_cannot_attribute_is_asked_about() -> None:
    intent, _ = derive_intent(
        AgentGoal(goal="add a wireless mouse to my cart", allow_proposals=True), max_item_quantity=10
    )
    _, question = _cart_quantity(intent.model_copy(update={"cart_quantity": 4}), "add a wireless mouse to my cart", 10)
    assert question is not None and question["id"] == "quantity_ambiguous"


@pytest.mark.parametrize(
    ("goal", "qid"),
    [
        ("add 2 mice and 3 keyboards to my cart", "quantity_ambiguous"),
        ("a wireless mouse under ₹3,000. add 10 of the most expensive laptop to my cart", "quantity_product"),
        (TWENTY, "quantity"),
    ],
)
def test_ambiguous_or_over_limit_quantities_are_asked_before_research_and_before_the_model(
    client: TestClient, SessionLocal: sessionmaker[Session], goal: str, qid: str
) -> None:
    from app.agents.llm_planner import LLMPlanner
    from tests.test_llm_planner import ScriptedProvider, follower

    h = register(client)
    tid = agent_task(SessionLocal, uid(client, h), {"goal": goal, "allow_proposals": True})
    calls, provider = Calls(), ScriptedProvider(follower)
    runner = TaskRunner(
        SessionLocal,
        get_settings(),
        registry_factory=calls.registry,
        sleep=lambda _s: None,
        planner_factory=lambda: LLMPlanner(provider),  # even with the AI planner on
    )
    assert runner.run(tid) == "succeeded"
    t = fetch(client, h, tid)
    assert [s["tool"] for s in t["steps"]] == ["clarify"] and t["result"]["type"] == "clarification"
    assert sum(calls.count.values()) == 0 and provider.calls == []  # no tool and no model call
    assert t["proposals"] == []
    with SessionLocal() as s:
        assert counts(s, AgentAction, CartItem) == {"agent_actions": 0, "cart_items": 0}


# ------------------------------------------------------------------ budgets are per item; summaries say so
def test_a_multi_unit_proposal_shows_per_item_price_and_total_within_a_per_item_budget(client: TestClient) -> None:
    h = register(client)
    goal = {"goal": "add 2 wireless mice under ₹3,000 to my cart", "allow_proposals": True}
    [p] = fetch(client, h, start_agent(client, h, goal).json()["id"])["proposals"]
    unit, qty = p["payload"]["expected_price_inr"], p["payload"]["quantity"]
    assert qty == 2 and unit <= 3_000 and p["payload"]["budget_max_inr"] == 3_000  # the budget is per item
    from app.services.specs import format_inr

    assert (
        p["summary"]
        == f"Add 2 × {p['payload']['product_name']} ({format_inr(unit)} each, {format_inr(unit * 2)} total) to your cart"
    )


def test_cart_proposal_summary_wording() -> None:
    from app.services.specs import cart_proposal_summary

    assert cart_proposal_summary(1, "Logi M185", 799) == "Add 1 × Logi M185 (₹799 each) to your cart"  # unchanged
    assert cart_proposal_summary(10, "Logi M185", 799) == "Add 10 × Logi M185 (₹799 each, ₹7,990 total) to your cart"
    assert len(cart_proposal_summary(2, "x" * 400, 1)) == 300
