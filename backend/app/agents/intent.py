"""Typed representation of what the shopper asked for.

Everything the agent does downstream (search, filtering, ranking, explanations, proposed
actions) is driven by a validated `ShoppingIntent` — never by free-form model output.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Op = Literal["lte", "gte", "eq", "neq", "in", "not_in"]
Scalar = float | int | bool | str
IntentKind = Literal[
    "search",  # find / recommend products
    "compare",  # compare named products
    "cart_add_reference",  # "add the second one to my cart"
    "save_preference",  # "remember that I prefer Lenovo"
    "reset",  # "start over"
    "help",  # greeting / capability question
]


class Constraint(BaseModel):
    attribute: str = Field(max_length=40)
    op: Op
    value: Scalar | list[str]
    hard: bool = True
    label: str = Field(default="", max_length=120)
    source: Literal["user", "preference", "context", "llm"] = "user"


class SoftPreference(BaseModel):
    """A 'nice to have' that influences ranking but never excludes products."""

    attribute: str = Field(max_length=40)
    direction: Literal["max", "min", "true"]
    weight: float = Field(default=1.0, ge=0, le=3)
    label: str = Field(default="", max_length=120)
    source: Literal["user", "preference", "context", "llm"] = "user"


class PreferenceProposal(BaseModel):
    kind: Literal["preferred_brand", "avoided_brand", "budget_max", "use_case", "note"]
    value: str = Field(max_length=200)
    category: str | None = None


class ShoppingIntent(BaseModel):
    kind: IntentKind = "search"
    category: str | None = None
    budget_max: int | None = Field(default=None, gt=0)
    budget_min: int | None = Field(default=None, ge=0)
    constraints: list[Constraint] = Field(default_factory=list)
    preferences: list[SoftPreference] = Field(default_factory=list)
    preferred_brands: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    wants_cart_add: bool = False
    cart_quantity: int = Field(default=1, ge=1)
    reference_index: int | None = Field(default=None, ge=0)
    sort_hint: Literal["cheapest", "best_rated"] | None = None
    compare_product_ids: list[int] = Field(default_factory=list)
    preference_to_save: PreferenceProposal | None = None
    inherited_from_context: bool = False

    def hard_constraints(self) -> list[Constraint]:
        """All exclusion rules, including budget, as explicit constraints."""
        out = [c for c in self.constraints if c.hard]
        if self.budget_max is not None:
            out.append(
                Constraint(
                    attribute="price_inr",
                    op="lte",
                    value=self.budget_max,
                    label=f"Price ≤ {_inr(self.budget_max)}",
                )
            )
        if self.budget_min is not None and self.budget_min > 0:
            out.append(
                Constraint(
                    attribute="price_inr",
                    op="gte",
                    value=self.budget_min,
                    label=f"Price ≥ {_inr(self.budget_min)}",
                )
            )
        return out

    def soft_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if not c.hard]


def _inr(v: int) -> str:
    from app.services.specs import format_inr

    return format_inr(v)
