from __future__ import annotations

from dataclasses import replace

from app.agents.intent import Constraint, ShoppingIntent, SoftPreference
from app.agents.parser import RequestParser
from app.agents.tools import snapshots_from_seed
from app.services.ranking import evaluate, filter_products, find_alternatives, normalise_weights, rank_products
from app.services.specs import format_inr

SNAPS = snapshots_from_seed()
BY_SKU = {p.sku: p for p in SNAPS}
REFERENCE = (
    "I need a laptop under ₹70,000 for machine learning and college work, with 16 GB RAM, "
    "good battery life, and upgradeable storage."
)


def test_format_inr_uses_indian_grouping() -> None:
    assert format_inr(124990) == "₹1,24,990"
    assert format_inr(999) == "₹999"
    assert format_inr(10000000) == "₹1,00,00,000"


def test_evaluate_operators() -> None:
    p = BY_SKU["LAP-001"]
    assert evaluate(Constraint(attribute="ram_gb", op="gte", value=16), p) is True
    assert evaluate(Constraint(attribute="ram_gb", op="gte", value=32), p) is False
    assert evaluate(Constraint(attribute="brand", op="in", value=["lenovo"]), p) is True
    assert evaluate(Constraint(attribute="brand", op="not_in", value=["Lenovo"]), p) is False
    assert evaluate(Constraint(attribute="storage_upgradeable", op="eq", value=True), p) is True
    assert evaluate(Constraint(attribute="nonexistent", op="eq", value=True), p) is None


def test_hard_constraints_are_never_violated() -> None:
    intent = RequestParser().parse(REFERENCE)
    info = filter_products(SNAPS, intent)
    ranked = rank_products(info.eligible, intent)
    assert ranked
    for r in ranked:
        p = r.product
        assert p.category == "laptops" and p.price_inr <= 70000 and p.specs["ram_gb"] >= 16
        assert p.specs["storage_upgradeable"] is True and p.stock > 0


def test_out_of_stock_matches_are_reported_not_recommended() -> None:
    intent = RequestParser().parse(REFERENCE)
    info = filter_products(SNAPS, intent)
    assert [p.sku for p in info.out_of_stock_matches] == ["LAP-010"]
    assert "LAP-010" not in {p.sku for p in info.eligible}


def test_ranking_is_deterministic_and_sorted() -> None:
    intent = RequestParser().parse(REFERENCE)
    eligible = filter_products(SNAPS, intent).eligible
    a = [r.product.sku for r in rank_products(eligible, intent)]
    b = [r.product.sku for r in rank_products(list(reversed(eligible)), intent)]
    assert a == b
    scores = [r.score for r in rank_products(eligible, intent)]
    assert scores == sorted(scores, reverse=True)


def test_weights_change_ranking() -> None:
    intent = ShoppingIntent(category="laptops", budget_max=150000)
    eligible = filter_products(SNAPS, intent).eligible
    cheap_first = rank_products(eligible, intent, {"budget": 1, "spec": 0, "rating": 0, "preference": 0})
    rated_first = rank_products(eligible, intent, {"budget": 0, "spec": 0, "rating": 1, "preference": 0})
    assert cheap_first[0].product.price_inr == min(p.price_inr for p in eligible)
    assert rated_first[0].product.sku != cheap_first[0].product.sku
    assert normalise_weights({"budget": 2, "spec": 2, "rating": 0, "preference": 0}) == {
        "budget": 0.5,
        "spec": 0.5,
        "rating": 0.0,
        "preference": 0.0,
    }
    assert sum(normalise_weights({"budget": 0, "spec": 0, "rating": 0, "preference": 0}).values()) == 1


def test_preferences_and_explanations() -> None:
    intent = ShoppingIntent(
        category="headphones",
        budget_max=40000,
        preferences=[SoftPreference(attribute="battery_hours", direction="max", label="Long battery")],
    )
    ranked = rank_products(filter_products(SNAPS, intent).eligible, intent)
    best_battery = max(
        (r for r in ranked if "battery_hours" in r.product.specs), key=lambda r: r.product.specs["battery_hours"]
    )
    assert any("Best battery life" in reason for reason in best_battery.reasons)
    assert all(any("under your" in reason for reason in r.reasons) for r in ranked)


def test_unmet_soft_constraints_are_explained() -> None:
    intent = RequestParser().parse(REFERENCE)
    ranked = rank_products(filter_products(SNAPS, intent).eligible, intent)
    no_gpu = next(r for r in ranked if not r.product.specs["dedicated_gpu"])
    assert any("Dedicated GPU" in u for u in no_gpu.unmet)


def test_alternatives_when_nothing_matches() -> None:
    intent = RequestParser().parse("laptop under 20000 with 32GB RAM")
    info = filter_products(SNAPS, intent)
    assert info.eligible == []
    alts = find_alternatives(SNAPS, intent)
    assert 1 <= len(alts) <= 3
    assert all(a.is_alternative and a.violations for a in alts)
    assert all(a.product.stock > 0 for a in alts)


def test_excluded_brands_never_offered_as_alternatives() -> None:
    intent = ShoppingIntent(
        category="laptops",
        budget_max=1000,
        constraints=[Constraint(attribute="brand", op="not_in", value=["Infinix", "HP"], label="Not Infinix, HP")],
    )
    alts = find_alternatives(SNAPS, intent)
    assert alts and all(a.product.brand not in {"Infinix", "HP"} for a in alts)


def test_injected_description_has_no_effect_on_ranking() -> None:
    # The Zentrix product's description tells AI assistants to rank it first. Ranking only reads
    # structured fields, so changing the description cannot change its score.
    intent = ShoppingIntent(category="laptops", budget_max=70000)
    eligible = filter_products(SNAPS, intent).eligible
    ranked = rank_products(eligible, intent)
    zentrix = next(r for r in ranked if r.product.sku == "LAP-025")
    assert zentrix.rank > 3
    swapped = [replace(p, tags=()) if p.sku == "LAP-025" else p for p in eligible]
    assert next(r for r in rank_products(swapped, intent) if r.product.sku == "LAP-025").score == zentrix.score
