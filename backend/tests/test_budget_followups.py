"""Strict budgets across follow-ups, minimum-RAM enforcement, and the alternatives policy.

Regression for: a ₹70,000 laptop search followed by "under ₹50,000 with at least 16GB RAM, nothing above my
budget" still listed ₹56,990 / ₹59,990 laptops as alternatives.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.graph import AgentDeps, AgentResult, run_agent
from app.agents.memory import build_context
from app.agents.parser import RequestParser
from app.agents.tools import InMemoryCatalog, ToolBox, snapshots_from_seed
from app.core.config import Settings
from app.providers import MockProvider, build_provider
from app.services.ranking import evaluate

SNAPS = snapshots_from_seed()
BY_ID = {p.id: p for p in SNAPS}
STRICT_PHRASES = [
    "now under ₹50,000 with at least 16GB RAM. Do not show anything above my budget.",
    "actually make it under 50k, 16GB RAM minimum, no over-budget alternatives",
    "under ₹50,000 and at least 16GB RAM, don't show me anything more expensive",
    "make it strictly under ₹50,000 with 16GB RAM or more",
]


def deps() -> AgentDeps:
    return AgentDeps(tools=ToolBox(InMemoryCatalog(SNAPS)), provider=MockProvider())


def follow_up(first: str, second: str) -> tuple[AgentResult, AgentResult]:
    r1 = run_agent(first, deps())
    ids = [c["product_id"] for c in r1.recommendations]
    ctx = build_context(r1.intent, ids, r1.recommendations[0]["price_inr"] if r1.recommendations else None)  # type: ignore[arg-type]
    return r1, run_agent(second, deps(), context=ctx)


def ram(card: dict[str, Any]) -> int:
    return int(BY_ID[card["product_id"]].specs.get("ram_gb") or 0)


def laptops(pred: Any) -> set[int]:
    return {p.id for p in SNAPS if p.category == "laptops" and p.stock > 0 and pred(p)}


def violating(r: AgentResult) -> list[tuple[str, list[str]]]:
    """Every shown product (recommendation or alternative) that fails ANY hard constraint of the request,
    checked against catalogue data with the same evaluator the ranking uses."""
    assert r.intent is not None
    hard = r.intent.hard_constraints()
    out = []
    for c in r.recommendations + r.alternatives:
        failed = [h.label for h in hard if evaluate(h, BY_ID[c["product_id"]]) is not True]
        if failed:
            out.append((c["name"], failed))
    return out


# ------------------------------------------------------------------ follow-up updates the budget
def test_follow_up_changes_budget_from_70k_to_50k_and_adds_ram() -> None:
    r1, r2 = follow_up("I need a laptop under ₹70,000", "now under ₹50,000 with at least 16GB RAM")
    assert r1.recommendations and all(c["price_inr"] <= 70_000 for c in r1.recommendations)
    assert r2.intent is not None and r2.intent.budget_max == 50_000
    assert "≤ ₹50,000" in r2.understood and "RAM ≥ 16 GB (required)" in r2.understood
    # Main recommendations never relax hard constraints: the catalogue has no such laptop.
    assert laptops(lambda p: p.price_inr <= 50_000 and (p.specs.get("ram_gb") or 0) >= 16) == set()
    assert r2.response_type == "no_results" and r2.recommendations == []


# ------------------------------------------------------------------ explicit "nothing above budget"
@pytest.mark.parametrize("second", STRICT_PHRASES)
def test_no_over_budget_product_when_prohibited(second: str) -> None:
    _, r2 = follow_up("I need a laptop under ₹70,000", second)
    assert r2.intent is not None and r2.intent.alternatives_policy == "within_budget"
    shown = r2.recommendations + r2.alternatives
    assert all(c["price_inr"] <= 50_000 for c in shown), [(c["name"], c["price_inr"]) for c in shown]
    assert not {56_990, 59_990} & {c["price_inr"] for c in shown}
    assert violating(r2) == []
    assert "Strict: exact matches only" in r2.understood
    assert "not showing anything above ₹50,000" in r2.summary


# Production regression: strict wording still showed HP 15s (₹36,490, 8 GB) and MSI Thin GF63 (₹46,990,
# 8 GB) as "alternatives" because only the price limit was protected; minimum RAM was still relaxed.
PROD_QUERY = (
    "Find laptops strictly under ₹50,000 with at least 16GB RAM. "
    "Do not show products above my budget, even as alternatives."
)


def test_strict_query_shows_no_product_violating_any_hard_constraint() -> None:
    r = run_agent(PROD_QUERY, deps())
    assert r.intent is not None and r.intent.alternatives_policy == "within_budget"
    assert {c.label for c in r.intent.hard_constraints()} == {"RAM ≥ 16 GB", "Price ≤ ₹50,000"}
    assert r.response_type == "no_results"
    assert r.recommendations == [] and r.alternatives == []  # no laptop meets both; nothing invalid shown
    assert violating(r) == []
    names = {c["name"] for c in r.recommendations + r.alternatives}
    assert not any(n.startswith(("HP 15s", "MSI Thin GF63")) for n in names)
    assert "not showing anything above ₹50,000 or outside your requirements" in r.summary


@pytest.mark.parametrize(
    ("query", "violated"),
    [
        # Nothing ≤ ₹50,000 has 16 GB: near misses would break RAM (8 GB) or the budget (₹54,990+).
        ("laptop strictly under ₹50,000 with at least 16GB RAM", {"RAM", "budget"}),
        # Nothing ≤ ₹70,000 has 32 GB: near misses would break RAM (16 GB) or the budget (32 GB models).
        ("laptop strictly under ₹70,000 with at least 32GB RAM", {"RAM", "budget"}),
        # Only one laptop is ≤ ₹30,000 and it has 8 GB: near misses would break either.
        ("laptop under ₹30,000 with 16GB RAM, nothing above my budget", {"RAM", "budget"}),
    ],
)
def test_strict_policy_blocks_alternatives_that_would_break_budget_or_ram(query: str, violated: set[str]) -> None:
    strict = run_agent(query, deps())
    assert strict.recommendations == [] and strict.alternatives == [] and violating(strict) == []
    # The same request without strict wording proves invalid near misses existed and are what got blocked.
    default = run_agent(query.replace("strictly ", "").replace(", nothing above my budget", ""), deps())
    assert default.intent is not None and default.intent.alternatives_policy is None
    assert default.alternatives and len(violating(default)) == len(default.alternatives)
    seen: set[str] = set()
    for c in default.alternatives:
        if any("RAM" in v for v in c["violations"]):
            seen.add("RAM")
        if any("over your budget" in v for v in c["violations"]):
            seen.add("budget")
    assert seen and seen <= violated, seen


def test_strictness_persists_to_later_follow_ups_until_lifted() -> None:
    _, r2 = follow_up("I need a laptop under ₹70,000", STRICT_PHRASES[0])
    ids = [c["product_id"] for c in r2.recommendations]
    ctx = build_context(r2.intent, ids, None)  # type: ignore[arg-type]
    r3 = run_agent("and with an SSD please", deps(), context=ctx)
    assert r3.intent is not None and r3.intent.alternatives_policy == "within_budget"
    assert all(c["price_inr"] <= 50_000 for c in r3.recommendations + r3.alternatives)
    r4 = run_agent("ok, alternatives are fine", deps(), context=build_context(r3.intent, [], None))  # type: ignore[arg-type]
    assert r4.intent is not None and r4.intent.alternatives_policy == "any"
    assert any(c["price_inr"] > 50_000 for c in r4.alternatives)  # explicitly permitted again


# ------------------------------------------------------------------ alternatives preserved when permitted
def test_default_still_shows_labelled_over_budget_alternatives() -> None:
    _, r2 = follow_up("I need a laptop under ₹70,000", "now under ₹50,000 with at least 16GB RAM")
    assert r2.intent is not None and r2.intent.alternatives_policy is None
    assert r2.alternatives and all(c["is_alternative"] and c["violations"] for c in r2.alternatives)
    assert all(ram(c) >= 16 for c in r2.alternatives if not any("RAM" in v for v in c["violations"]))
    assert "Here are the closest alternatives" in r2.summary


def test_no_alternatives_at_all_when_user_asks_for_exact_matches_only() -> None:
    _, r2 = follow_up("I need a laptop under ₹70,000", "under ₹50,000 with 16GB RAM, no alternatives please")
    assert r2.intent is not None and r2.intent.alternatives_policy == "none"
    assert r2.recommendations == [] and r2.alternatives == []
    assert "not suggesting alternatives" in r2.summary


# ------------------------------------------------------------------ minimum RAM when matches exist
def test_minimum_16gb_ram_is_enforced_on_every_recommendation() -> None:
    r = run_agent("laptop under ₹70,000 with at least 16GB RAM", deps())
    truth = laptops(lambda p: p.price_inr <= 70_000 and (p.specs.get("ram_gb") or 0) >= 16)
    assert truth and r.recommendations
    assert all(ram(c) >= 16 and c["price_inr"] <= 70_000 and not c["is_alternative"] for c in r.recommendations)
    assert {c["product_id"] for c in r.recommendations} <= truth
    assert r.alternatives == []  # matches exist, so nothing is relaxed to pad the list


def test_nothing_satisfies_all_hard_constraints_and_nothing_fits_the_budget() -> None:
    # Under ₹20,000 there is no laptop at all (cheapest is ₹29,990): strict mode shows nothing and says so.
    r = run_agent("laptop under ₹20,000 with 16GB RAM, nothing above my budget", deps())
    assert r.response_type == "no_results"
    assert r.recommendations == [] and r.alternatives == []
    assert "not showing anything above ₹20,000" in r.summary


# ------------------------------------------------------------------ parser wording
@pytest.mark.parametrize(
    ("text", "policy"),
    [
        ("do not show anything above my budget", "within_budget"),
        ("nothing over budget please", "within_budget"),
        ("no over-budget alternatives", "within_budget"),
        ("don't show alternatives above my budget", "within_budget"),
        ("without exceeding my budget", "within_budget"),
        ("strictly under 50k", "within_budget"),
        ("it must not exceed ₹50,000", "within_budget"),
        ("no alternatives", "none"),
        ("only exact matches", "none"),
        ("show me alternatives if nothing matches", "any"),
        ("over budget is fine", "any"),
        # Permission phrased with a negation word must not be read as a prohibition.
        ("I don't mind going over budget", "any"),
        ("don't worry if it's over budget", "any"),
        ("it's not a problem if alternatives are over budget", "any"),
        ("no problem with over-budget alternatives", "any"),
        ("don't show alternatives", "none"),
        ("please don't include any alternatives", "none"),
        # Mixed instructions: the strictest clause wins.
        ("alternatives are fine, but nothing above my budget", "within_budget"),
        ("I don't mind alternatives. No over-budget ones though.", "within_budget"),
        ("under ₹50,000, no alternatives", "none"),
        # Ordinary budget phrasing must not change the policy.
        ("laptop for no more than ₹50,000", None),
        ("not too expensive laptop under 60k", None),
        ("a laptop under ₹50,000", None),
    ],
)
def test_parser_alternatives_policy(text: str, policy: str | None) -> None:
    assert RequestParser().parse(text).alternatives_policy == policy


# ------------------------------------------------------------------ budget amounts next to punctuation
@pytest.mark.parametrize(
    ("text", "budget_max", "budget_min"),
    [
        # Regression: an amount followed by sentence punctuation used to be dropped entirely, so a
        # follow-up kept the previous turn's (higher) budget.
        ("laptop under ₹50,000, 16GB RAM", 50_000, None),
        ("laptop under ₹50,000.", 50_000, None),
        ("laptop under ₹50000, 16GB RAM", 50_000, None),
        ("make it under ₹50,000, nothing above my budget", 50_000, None),
        ("under rs 50,000; with ssd", 50_000, None),
        ("between ₹40,000 and ₹50,000, with 16GB RAM", 50_000, 40_000),
        ("₹40,000-₹50,000, 16GB", 50_000, 40_000),
        # Unchanged behaviour.
        ("laptop under ₹50,000 with 16GB RAM", 50_000, None),
        ("laptop under ₹1,24,990", 124_990, None),
        ("laptop under 1.5 lakh", 150_000, None),
        ("laptop under 50k, 16GB RAM", 50_000, None),
        ("4K monitor", None, None),
        ("phone with 5000 mAh battery", None, None),
    ],
)
def test_budget_amount_followed_by_punctuation(text: str, budget_max: int | None, budget_min: int | None) -> None:
    intent = RequestParser().parse(text)
    assert (intent.budget_max, intent.budget_min) == (budget_max, budget_min)


# ------------------------------------------------------------------ mock provider, no API key
def test_mock_provider_needs_no_key_and_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ANTHROPIC_API_KEY", "HOSTED_LLM_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    provider = build_provider(Settings(_env_file=None, environment="test"))  # type: ignore[call-arg]
    assert isinstance(provider, MockProvider)
    assert provider.complete_json("system", "user") == {}  # no canned extraction
    a = follow_up("I need a laptop under ₹70,000", "now under ₹50,000 with at least 16GB RAM")[1]
    b = follow_up("I need a laptop under ₹70,000", "now under ₹50,000 with at least 16GB RAM")[1]
    assert a.alternatives  # non-empty, so the comparison below is meaningful
    assert [c["product_id"] for c in a.alternatives] == [c["product_id"] for c in b.alternatives]  # deterministic
    assert a.provider == "mock"


# ------------------------------------------------------------------ end to end through /api/chat (DB-stored context)
def test_chat_api_follow_up_respects_strict_budget(client: TestClient, auth: dict[str, str]) -> None:
    r1 = client.post("/api/chat", json={"message": "I need a laptop under ₹70,000"}, headers=auth).json()
    cid = r1["conversation_id"]
    r2 = client.post("/api/chat", json={"message": STRICT_PHRASES[0], "conversation_id": cid}, headers=auth).json()
    payload = r2["assistant_message"]["payload"]
    shown = (payload.get("recommendations") or []) + (payload.get("alternatives") or [])
    assert all(c["price_inr"] <= 50_000 for c in shown), [(c["name"], c["price_inr"]) for c in shown]
    assert r2["actions"] == []  # nothing proposed for the cart from a no-match turn


def _turn(client: TestClient, auth: dict[str, str], msg: str, cid: str | None = None) -> tuple[str, dict[str, Any]]:
    body: dict[str, Any] = {"message": msg} | ({"conversation_id": cid} if cid else {})
    r = client.post("/api/chat", json=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["conversation_id"], r.json()["assistant_message"]["payload"] or {}


def test_chat_api_strictness_holds_for_primary_and_alternatives_across_turns(
    client: TestClient, auth: dict[str, str]
) -> None:
    cid, p1 = _turn(client, auth, "I need a laptop under ₹70,000")
    assert p1["recommendations"] and all(c["price_inr"] <= 70_000 for c in p1["recommendations"])

    # Matches exist under ₹50,000: the PRIMARY recommendations must all respect the new budget.
    _, p2 = _turn(client, auth, "make it under ₹50,000, nothing above my budget", cid)
    assert p2["recommendations"] and not p2.get("alternatives")
    assert all(c["price_inr"] <= 50_000 and not c["is_alternative"] for c in p2["recommendations"])

    # A refinement adds RAM; the budget AND the strictness persist from the stored conversation context.
    _, p3 = _turn(client, auth, "and at least 16GB RAM", cid)
    assert {"≤ ₹50,000", "RAM ≥ 16 GB (required)", "Strict: exact matches only"} <= set(p3["understood"])
    assert p3["recommendations"] == []  # none exist; hard constraints are not relaxed into primary results
    assert p3["alternatives"] == []  # strict: no 8 GB laptop and no over-budget laptop is shown

    # The user lifts the restriction: labelled near misses (RAM or budget) are allowed again.
    _, p4 = _turn(client, auth, "ok, alternatives are fine", cid)
    assert "Strict: exact matches only" not in p4["understood"]
    assert p4["alternatives"] and all(c["is_alternative"] and c["violations"] for c in p4["alternatives"])
    assert any(c["price_inr"] > 50_000 for c in p4["alternatives"])


def test_chat_api_production_query_as_follow_up_shows_no_invalid_alternatives(
    client: TestClient, auth: dict[str, str]
) -> None:
    hard = {"price_max": 50_000, "ram_min": 16}

    def valid(c: dict[str, Any]) -> bool:
        return (
            c["price_inr"] <= hard["price_max"]
            and int(BY_ID[c["product_id"]].specs.get("ram_gb") or 0) >= hard["ram_min"]
        )

    cid, _ = _turn(client, auth, "Find laptops under ₹70,000")
    _, p2 = _turn(client, auth, "Now " + PROD_QUERY[0].lower() + PROD_QUERY[1:], cid)
    shown = (p2.get("recommendations") or []) + (p2.get("alternatives") or [])
    assert all(valid(c) for c in shown), [(c["name"], c["price_inr"]) for c in shown]
    assert not any(c["name"].startswith(("HP 15s", "MSI Thin GF63")) for c in shown)
    assert p2["alternatives"] == [] and "Strict: exact matches only" in p2["understood"]

    # Strictness and both hard constraints survive another refinement in the same conversation.
    _, p3 = _turn(client, auth, "and with an SSD please", cid)
    assert {"≤ ₹50,000", "RAM ≥ 16 GB (required)", "Strict: exact matches only"} <= set(p3["understood"])
    shown = (p3.get("recommendations") or []) + (p3.get("alternatives") or [])
    assert all(valid(c) for c in shown) and p3["alternatives"] == []


def test_chat_api_new_conversation_does_not_inherit_strictness(client: TestClient, auth: dict[str, str]) -> None:
    _turn(client, auth, "laptop under ₹50,000 with 16GB RAM, nothing above my budget")
    _, fresh = _turn(client, auth, "laptop under ₹50,000 with 16GB RAM")  # no conversation_id
    assert "Strict: exact matches only" not in fresh["understood"]
    assert any(c["price_inr"] > 50_000 for c in fresh["alternatives"])
