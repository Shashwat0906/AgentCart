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

from app.agents.planner import AgentGoal, derive_intent
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
