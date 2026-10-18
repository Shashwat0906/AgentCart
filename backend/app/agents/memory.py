"""Short-term conversation context vs. long-term saved preferences.

* Conversation context (per conversation, automatic): last validated intent + last
  recommendation ids, so follow-ups like "cheaper ones" or "add the second one" work.
* Saved preferences (per user, explicit): only written after the user approves, and they
  only ever add *soft* signals or brand exclusions the user chose.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.intent import Constraint, ShoppingIntent, SoftPreference
from app.services.specs import CATEGORY_LABELS, format_inr

_REFINEMENT_CUE = re.compile(r"^(and|also|with|but|what about|how about|make it|only|now|instead|ok(ay)?|actually|same but|show me (ones|those|options)|ones?\b|something)\b", re.I)


def merge_with_context(intent: ShoppingIntent, context: dict[str, Any], text: str) -> ShoppingIntent:
    prev_data = context.get("intent")
    if intent.kind != "search" or not prev_data:
        return intent
    prev = ShoppingIntent.model_validate(prev_data)
    same_or_missing = intent.category is None or intent.category == prev.category
    refinement = intent.category is None or bool(_REFINEMENT_CUE.search(text.strip()))
    if not (same_or_missing and refinement and prev.category):
        return intent

    merged = prev.model_copy(deep=True)
    merged.kind = "search"
    merged.inherited_from_context = True
    merged.wants_cart_add = intent.wants_cart_add
    merged.cart_quantity = intent.cart_quantity
    merged.reference_index = intent.reference_index
    merged.preference_to_save = intent.preference_to_save
    merged.keywords = intent.keywords
    new_keys = {(c.attribute, c.op) for c in intent.constraints}
    new_attrs_eq = {c.attribute for c in intent.constraints if c.op in {"eq", "in", "not_in"}}
    merged.constraints = [
        c for c in merged.constraints
        if (c.attribute, c.op) not in new_keys and not (c.attribute in new_attrs_eq and c.op in {"eq", "in", "not_in"})
    ] + intent.constraints
    pref_attrs = {p.attribute for p in intent.preferences}
    merged.preferences = [p for p in merged.preferences if p.attribute not in pref_attrs] + intent.preferences
    if intent.budget_max is not None or intent.budget_min is not None:
        merged.budget_max, merged.budget_min = intent.budget_max, intent.budget_min
    if intent.preferred_brands:
        merged.preferred_brands = intent.preferred_brands
    merged.use_cases = list(dict.fromkeys(merged.use_cases + intent.use_cases))
    if intent.sort_hint:
        merged.sort_hint = intent.sort_hint
    last_top = context.get("last_top_price")
    if "__cheaper__" in intent.keywords and isinstance(last_top, int) and intent.budget_max is None:
        merged.budget_max = min(last_top - 1, merged.budget_max or last_top)
        merged.budget_min = None
    return merged


def apply_saved_preferences(intent: ShoppingIntent, prefs: list[dict[str, Any]]) -> tuple[ShoppingIntent, list[str]]:
    if intent.kind != "search" or not prefs:
        return intent, []
    applied: list[str] = []
    it = intent.model_copy(deep=True)
    mentioned_brands = {
        str(v).lower()
        for c in it.constraints if c.attribute == "brand" and c.op == "in"
        for v in (c.value if isinstance(c.value, list) else [c.value])
    }
    avoided: list[str] = []
    for p in prefs:
        kind, value, category = p.get("kind"), str(p.get("value", "")), p.get("category")
        if category and it.category and category != it.category:
            continue
        if kind == "preferred_brand" and value and value.lower() not in {b.lower() for b in it.preferred_brands}:
            it.preferred_brands.append(value)
            applied.append(f"Prefers {value}")
        elif kind == "avoided_brand" and value and value.lower() not in mentioned_brands:
            avoided.append(value)
        elif kind == "budget_max" and it.budget_max is None and category == it.category and value.isdigit():
            it.budget_max = int(value)
            applied.append(f"Saved budget for {CATEGORY_LABELS.get(category or '', category or '')}: ≤ {format_inr(int(value))}")
        elif kind == "use_case" and value and value not in it.use_cases:
            it.use_cases.append(value)
            applied.append(f"Usually shops for: {value}")
            if value == "machine learning" and it.category == "laptops" and not any(c.attribute == "dedicated_gpu" for c in it.constraints):
                it.constraints.append(Constraint(attribute="dedicated_gpu", op="eq", value=True, hard=False,
                                                 label="Dedicated GPU (saved use case)", source="preference"))
            if value == "travel" and it.category == "laptops" and not any(pr.attribute == "weight_kg" for pr in it.preferences):
                it.preferences.append(SoftPreference(attribute="weight_kg", direction="min", label="Lightweight (saved use case)", source="preference"))
    if avoided:
        it.constraints.append(Constraint(attribute="brand", op="not_in", value=avoided,
                                         label="Not " + ", ".join(avoided), source="preference"))
        applied.append("Avoids " + ", ".join(avoided))
    return it, applied


def build_context(intent: ShoppingIntent, recommendation_ids: list[int], top_price: int | None) -> dict[str, Any]:
    stored = intent.model_copy(update={"wants_cart_add": False, "reference_index": None,
                                       "preference_to_save": None, "keywords": [], "cart_quantity": 1})
    return {"intent": stored.model_dump(mode="json"), "last_recommendation_ids": recommendation_ids,
            "last_top_price": top_price}
