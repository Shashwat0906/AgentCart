"""Deterministic constraint filtering, scoring and explanation.

Pipeline:  hard filters  →  scoring (budget / spec / rating / preference)  →  explanations.
The same input always produces the same ranking. No LLM is involved here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agents.intent import Constraint, ShoppingIntent, SoftPreference
from app.services.specs import format_inr, format_spec_value, spec_field

DEFAULT_WEIGHTS = {"budget": 0.25, "spec": 0.30, "rating": 0.20, "preference": 0.25}
RATING_PRIOR = 4.0
RATING_PRIOR_WEIGHT = 50
LOW_STOCK_THRESHOLD = 3


class ProductLike(Protocol):
    """Read-only view satisfied by both ORM `Product` rows and frozen `ProductSnapshot`s."""

    @property
    def id(self) -> int: ...
    @property
    def sku(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def brand(self) -> str: ...
    @property
    def category(self) -> str: ...
    @property
    def price_inr(self) -> int: ...
    @property
    def rating(self) -> float: ...
    @property
    def review_count(self) -> int: ...
    @property
    def stock(self) -> int: ...
    @property
    def specs(self) -> dict[str, Any]: ...


@dataclass
class RankedProduct:
    product: ProductLike
    score: float
    breakdown: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    tradeoffs: list[str] = field(default_factory=list)
    unmet: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)  # only for alternatives
    rank: int = 0
    is_alternative: bool = False


@dataclass
class FilterResult:
    eligible: list[ProductLike]
    out_of_stock_matches: list[ProductLike]
    exclusion_counts: dict[str, int]
    considered: int


# --------------------------------------------------------------------------- helpers


def get_attr(product: ProductLike, attribute: str) -> Any:
    if attribute in {"price_inr", "rating", "brand", "stock", "review_count"}:
        return getattr(product, attribute)
    return (product.specs or {}).get(attribute)


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    return None


def evaluate(constraint: Constraint, product: ProductLike) -> bool | None:
    """True/False if the product satisfies the constraint; None if the attribute is unknown."""
    actual = get_attr(product, constraint.attribute)
    if actual is None:
        return None
    target = constraint.value
    op = constraint.op
    if op in {"lte", "gte"}:
        a, t = _num(actual), _num(target)
        if a is None or t is None:
            return None
        return a <= t if op == "lte" else a >= t
    if op in {"in", "not_in"}:
        values = target if isinstance(target, list) else [target]
        hit = str(actual).lower() in {str(v).lower() for v in values}
        return hit if op == "in" else not hit
    if isinstance(actual, bool) or isinstance(target, bool):
        equal = bool(actual) == bool(target)
    elif _num(actual) is not None and _num(target) is not None:
        equal = _num(actual) == _num(target)
    else:
        equal = str(actual).strip().lower() == str(target).strip().lower()
    return equal if op == "eq" else not equal


def normalise_weights(weights: dict[str, float] | None) -> dict[str, float]:
    w = {k: max(0.0, float((weights or {}).get(k, v))) for k, v in DEFAULT_WEIGHTS.items()}
    total = sum(w.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in w.items()}


def describe(product: ProductLike, attribute: str) -> str:
    f = spec_field(product.category, attribute)
    value = get_attr(product, attribute)
    if f is None:
        return str(value)
    return format_spec_value(f, value)


def _attr_label(category: str | None, attribute: str) -> str:
    f = spec_field(category, attribute)
    return f.label if f else attribute.replace("_", " ")


# --------------------------------------------------------------------------- filtering


def filter_products(products: Sequence[ProductLike], intent: ShoppingIntent) -> FilterResult:
    hard = intent.hard_constraints()
    eligible: list[ProductLike] = []
    oos: list[ProductLike] = []
    exclusions: Counter[str] = Counter()
    considered = 0
    for p in products:
        if intent.category and p.category != intent.category:
            continue
        considered += 1
        failed = [c for c in hard if evaluate(c, p) is not True]
        if failed:
            for c in failed:
                exclusions[c.label or c.attribute] += 1
            continue
        if p.stock <= 0:
            oos.append(p)
            continue
        eligible.append(p)
    return FilterResult(eligible, oos, dict(exclusions), considered)


# --------------------------------------------------------------------------- scoring


def _minmax(values: list[float]) -> tuple[float, float]:
    return (min(values), max(values)) if values else (0.0, 0.0)


def _norm(v: float, lo: float, hi: float, higher_is_better: bool = True) -> float:
    if hi - lo < 1e-9:
        return 1.0
    x = (v - lo) / (hi - lo)
    return x if higher_is_better else 1.0 - x


def _bayes_rating(p: ProductLike) -> float:
    n = max(0, p.review_count)
    return (p.rating * n + RATING_PRIOR * RATING_PRIOR_WEIGHT) / (n + RATING_PRIOR_WEIGHT)


def _pref_score(pref: SoftPreference, p: ProductLike, ranges: dict[str, tuple[float, float]]) -> float | None:
    v = get_attr(p, pref.attribute)
    if pref.direction == "true":
        return None if v is None else (1.0 if bool(v) else 0.0)
    num = _num(v)
    if num is None:
        return 0.0  # unknown counts as worst for "more/less is better" preferences
    lo, hi = ranges.get(pref.attribute, (num, num))
    return _norm(num, lo, hi, higher_is_better=pref.direction == "max")


def rank_products(
    products: Sequence[ProductLike],
    intent: ShoppingIntent,
    weights: dict[str, float] | None = None,
    *,
    limit: int | None = None,
) -> list[RankedProduct]:
    if not products:
        return []
    w = dict(normalise_weights(weights))
    if intent.sort_hint == "cheapest":
        w["budget"] *= 3
    elif intent.sort_hint == "best_rated":
        w["rating"] *= 3
    w = normalise_weights(w)

    prices = [float(p.price_inr) for p in products]
    p_lo, p_hi = _minmax(prices)
    ranges: dict[str, tuple[float, float]] = {}
    for pref in intent.preferences:
        if pref.direction != "true":
            vals = [n for p in products if (n := _num(get_attr(p, pref.attribute))) is not None]
            ranges[pref.attribute] = _minmax(vals)
    hard_numeric = [c for c in intent.constraints if c.hard and c.op == "gte" and _num(c.value) not in (None, 0)]
    soft = intent.soft_constraints()
    preferred = {b.lower() for b in intent.preferred_brands}

    ranked: list[RankedProduct] = []
    for p in products:
        # Budget fit: cheaper is better, scaled against the budget (or the shortlist range).
        if intent.budget_max:
            budget = max(0.0, min(1.0, 1.0 - 0.5 * (p.price_inr / intent.budget_max)))
        else:
            budget = 0.5 + 0.5 * _norm(p.price_inr, p_lo, p_hi, higher_is_better=False)

        # Spec match: soft constraints met + headroom over hard numeric minimums.
        spec_parts: list[float] = [1.0 if evaluate(c, p) is True else 0.0 for c in soft]
        for c in hard_numeric:
            actual = _num(get_attr(p, c.attribute))
            req = _num(c.value) or 1.0
            if actual is not None:
                spec_parts.append(min(1.0, 0.5 + 0.5 * max(0.0, (actual - req) / req)))
        spec = sum(spec_parts) / len(spec_parts) if spec_parts else 0.5

        rating = max(0.0, min(1.0, (_bayes_rating(p) - 3.0) / 2.0))

        pref_parts: list[tuple[float, float]] = []
        for pref in intent.preferences:
            s = _pref_score(pref, p, ranges)
            if s is not None:
                pref_parts.append((s, pref.weight))
        if preferred:
            pref_parts.append((1.0 if p.brand.lower() in preferred else 0.0, 1.0))
        total_w = sum(wt for _, wt in pref_parts)
        preference = sum(s * wt for s, wt in pref_parts) / total_w if total_w else 0.5

        breakdown = {
            "budget": round(budget, 4),
            "spec": round(spec, 4),
            "rating": round(rating, 4),
            "preference": round(preference, 4),
        }
        score = 100 * sum(w[k] * breakdown[k] for k in w)
        ranked.append(RankedProduct(product=p, score=round(score, 2), breakdown=breakdown))

    ranked.sort(key=lambda r: (-r.score, r.product.price_inr, r.product.id))
    if "__cheapest__" in intent.keywords:
        # "the cheapest X" means the lowest price among matches, not just a price-leaning blend.
        ranked.sort(key=lambda r: (r.product.price_inr, -r.score, r.product.id))
    for i, r in enumerate(ranked, start=1):
        r.rank = i
        _explain(r, intent, products, ranges)
    return ranked[:limit] if limit else ranked


# --------------------------------------------------------------------------- explanations


def _explain(
    r: RankedProduct,
    intent: ShoppingIntent,
    shortlist: Sequence[ProductLike],
    ranges: dict[str, tuple[float, float]],
) -> None:
    p = r.product
    if intent.budget_max:
        diff = intent.budget_max - p.price_inr
        r.reasons.append(
            f"{format_inr(diff)} under your {format_inr(intent.budget_max)} budget"
            if diff > 0
            else "Exactly at your budget"
        )
    for c in intent.constraints:
        if c.hard and c.attribute not in {"brand", "price_inr", "rating"}:
            r.reasons.append(f"{_attr_label(p.category, c.attribute)}: {describe(p, c.attribute)} ✓")
    for c in intent.soft_constraints():
        if evaluate(c, p) is True:
            r.reasons.append(f"{c.label or c.attribute} ✓")
        else:
            r.unmet.append(
                f"{c.label or c.attribute} — not met ({_attr_label(p.category, c.attribute)}: {describe(p, c.attribute)})"
            )
    for pref in intent.preferences:
        if pref.direction == "true":
            continue
        num = _num(get_attr(p, pref.attribute))
        lo, hi = ranges.get(pref.attribute, (0.0, 0.0))
        if num is None or len(shortlist) < 2 or hi - lo < 1e-9:
            continue
        best = hi if pref.direction == "max" else lo
        rel = _norm(num, lo, hi, higher_is_better=pref.direction == "max")
        label = _attr_label(p.category, pref.attribute).lower()
        value = describe(p, pref.attribute)
        best_field = spec_field(p.category, pref.attribute)
        best_desc = format_spec_value(best_field, best) if best_field else str(best)
        higher = pref.direction == "max"
        if num == best:
            r.reasons.append(f"{'Best' if higher else 'Lowest'} {label} in this shortlist ({value})")
        elif rel >= 0.7:
            r.reasons.append(f"{'Strong' if higher else 'Low'} {label} ({value})")
        elif rel <= 0.3:
            r.tradeoffs.append(
                f"Weaker {label}: {value} vs up to {best_desc}"
                if higher
                else f"Higher {label}: {value} vs as low as {best_desc}"
            )
    if intent.preferred_brands and p.brand.lower() in {b.lower() for b in intent.preferred_brands}:
        r.reasons.append(f"From {p.brand}, a brand you prefer")
    r.reasons.append(f"Rated {p.rating:.1f}★ from {p.review_count:,} reviews")
    if p.rating < 4.0:
        r.tradeoffs.append(f"Below-average rating ({p.rating:.1f}★)")
    if 0 < p.stock <= LOW_STOCK_THRESHOLD:
        r.tradeoffs.append(f"Only {p.stock} left in stock")


# --------------------------------------------------------------------------- alternatives


def _violation(c: Constraint, p: ProductLike) -> tuple[str, float]:
    """Human-readable description of how a product misses a hard constraint, plus a gap size."""
    actual = get_attr(p, c.attribute)
    label = _attr_label(p.category, c.attribute)
    if actual is None:
        return f"{label} unknown (you required {c.label or c.attribute})", 1.0
    if c.attribute == "price_inr" and c.op == "lte":
        over = p.price_inr - int(_num(c.value) or 0)
        return f"{format_inr(over)} over your budget", over / max(1.0, _num(c.value) or 1.0)
    a, t = _num(actual), _num(c.value)
    if a is not None and t is not None and c.op in {"gte", "lte"}:
        gap = abs(a - t) / max(abs(t), 1e-9)
        return f"{label}: {describe(p, c.attribute)} (you asked {c.label or c.attribute})", gap
    return f"{label}: {describe(p, c.attribute)} (you asked {c.label or c.attribute})", 1.0


def find_alternatives(
    products: Sequence[ProductLike],
    intent: ShoppingIntent,
    weights: dict[str, float] | None = None,
    limit: int = 3,
) -> list[RankedProduct]:
    """Closest in-stock products that miss one or more hard constraints, clearly labelled.

    Never relaxed: brand exclusions, and, when `alternatives_policy == "within_budget"`, the price range.
    `alternatives_policy == "none"` returns nothing.
    """
    if intent.alternatives_policy == "none":
        return []
    hard = intent.hard_constraints()
    never_relax = [
        c
        for c in hard
        if (c.attribute == "brand" and c.op == "not_in")
        or (intent.alternatives_policy == "within_budget" and c.attribute == "price_inr")
    ]
    scored: list[tuple[int, float, float, ProductLike, list[str]]] = []
    for p in products:
        if p.stock <= 0 or (intent.category and p.category != intent.category):
            continue
        # The user explicitly ruled these out, so a near miss here is not an alternative.
        if any(evaluate(c, p) is not True for c in never_relax):
            continue
        failed = [c for c in hard if evaluate(c, p) is not True]
        if not failed:
            continue
        details = [_violation(c, p) for c in failed]
        scored.append((len(failed), sum(g for _, g in details), -p.rating, p, [d for d, _ in details]))
    scored.sort(key=lambda t: (t[0], round(t[1], 4), t[2], t[3].id))
    picked = scored[:limit]
    if not picked:
        return []
    relaxed = intent.model_copy(
        update={"budget_max": None, "budget_min": None, "constraints": [c for c in intent.constraints if not c.hard]}
    )
    ranked = rank_products([t[3] for t in picked], relaxed, weights)
    by_id = {t[3].id: t[4] for t in picked}
    order = {t[3].id: i for i, t in enumerate(picked)}
    ranked.sort(key=lambda r: order[r.product.id])
    for i, r in enumerate(ranked, start=1):
        r.rank = i
        r.is_alternative = True
        r.violations = by_id[r.product.id]
    return ranked
