"""Validate and normalise an extracted ShoppingIntent against the spec registry."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.intent import Constraint, ShoppingIntent
from app.services.specs import CATEGORY_LABELS, CATEGORY_SPECS, format_inr, spec_field

MAX_CONSTRAINTS = 15
MAX_BUDGET = 10_000_000
_OPS_BY_TYPE = {"number": {"lte", "gte", "eq"}, "bool": {"eq", "neq"}, "text": {"eq", "neq", "in", "not_in"}}


@dataclass
class ValidationResult:
    intent: ShoppingIntent
    issues: list[str] = field(default_factory=list)  # non-blocking notes shown to the user
    questions: list[dict[str, object]] = field(default_factory=list)  # blocking clarifications
    soft_questions: list[dict[str, object]] = field(default_factory=list)  # optional follow-ups


def _valid_constraint(c: Constraint, category: str | None, issues: list[str]) -> Constraint | None:
    f = spec_field(category, c.attribute)
    if f is None:
        if c.source == "llm":
            issues.append(f"Ignored an unsupported requirement ('{c.attribute}').")
        return None
    if c.op not in _OPS_BY_TYPE[f.type]:
        return None
    value = c.value
    if f.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            try:
                value = float(str(value))
            except ValueError:
                return None
        if (f.min_value is not None and value < f.min_value * 0.5) or (f.max_value is not None and value > f.max_value * 1.5):
            issues.append(f"Ignored '{c.label or c.attribute}' — outside any realistic range for {CATEGORY_LABELS.get(category or '', 'this category').lower()}.")
            return None
    elif f.type == "bool":
        if not isinstance(value, bool):
            value = str(value).strip().lower() in {"true", "yes", "1"}
    else:
        value = [str(v)[:60] for v in value] if isinstance(value, list) else str(value)[:60]
    return c.model_copy(update={"value": value})


def validate_intent(intent: ShoppingIntent, *, max_item_quantity: int) -> ValidationResult:
    res = ValidationResult(intent=intent.model_copy(deep=True))
    it = res.intent
    if it.category is not None and it.category not in CATEGORY_SPECS:
        res.issues.append(f"Unknown category '{it.category}' ignored.")
        it.category = None

    if it.budget_max is not None and not (0 < it.budget_max <= MAX_BUDGET):
        res.issues.append("Ignored an unrealistic budget.")
        it.budget_max = None
    if it.budget_min is not None and not (0 <= it.budget_min <= MAX_BUDGET):
        it.budget_min = None
    if it.budget_min and it.budget_max and it.budget_min > it.budget_max:
        res.questions.append({
            "id": "budget_conflict",
            "question": f"Your minimum ({format_inr(it.budget_min)}) is higher than your maximum ({format_inr(it.budget_max)}). What budget should I use?",
            "options": [f"Up to {format_inr(it.budget_max)}", f"Up to {format_inr(it.budget_min)}"],
        })

    cleaned: list[Constraint] = []
    for c in it.constraints[:MAX_CONSTRAINTS]:
        v = _valid_constraint(c, it.category, res.issues)
        if v is not None:
            cleaned.append(v)
    if len(it.constraints) > MAX_CONSTRAINTS:
        res.issues.append(f"Only the first {MAX_CONSTRAINTS} requirements were used.")
    it.constraints = cleaned
    it.preferences = [p for p in it.preferences if spec_field(it.category, p.attribute) is not None]

    # Contradictory numeric bounds, e.g. RAM ≥ 32 and RAM ≤ 16.
    lows = {c.attribute: float(c.value) for c in cleaned if c.hard and c.op == "gte" and isinstance(c.value, int | float)}
    for c in cleaned:
        if c.hard and c.op == "lte" and isinstance(c.value, int | float) and c.attribute in lows and lows[c.attribute] > c.value:
            label = spec_field(it.category, c.attribute)
            res.questions.append({
                "id": f"conflict_{c.attribute}",
                "question": f"Your {label.label.lower() if label else c.attribute} requirements contradict each other. Which limit matters more?",
                "options": [],
            })

    if it.cart_quantity > max_item_quantity:
        res.issues.append(f"You can add at most {max_item_quantity} units of one product.")

    if it.kind == "search" and it.category is None:
        res.questions.append({
            "id": "category",
            "question": "What kind of product are you shopping for?",
            "options": list(CATEGORY_LABELS.values()),
        })
    elif it.kind == "search" and it.budget_max is None and it.budget_min is None:
        res.soft_questions.append({
            "id": "budget",
            "question": "Do you have a budget in mind? I can narrow these down further.",
            "options": [],
        })
    return res
