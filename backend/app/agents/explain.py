"""Turn ranked results into grounded, human-readable output."""

from __future__ import annotations

import json
from typing import Any

from app.agents.guardrails import build_facts, check_grounding
from app.agents.intent import ShoppingIntent
from app.providers.base import LLMProvider, ProviderError
from app.services.catalog import ProductSnapshot, key_specs
from app.services.ranking import FilterResult, RankedProduct
from app.services.specs import CATEGORY_LABELS, format_inr

POLISH_SYSTEM = """You rewrite a shopping assistant's draft reply to sound friendly and concise (max 90 words).
Use ONLY facts present in FACTS. Do not add any number, price, rating, specification or product that
is not in FACTS. Do not include links. Keep product names exactly as written. Return plain text."""


def product_card(r: RankedProduct) -> dict[str, Any]:
    p = r.product
    return {
        "product_id": p.id,
        "sku": p.sku,
        "name": p.name,
        "brand": p.brand,
        "category": p.category,
        "price_inr": p.price_inr,
        "mrp_inr": getattr(p, "mrp_inr", p.price_inr),
        "rating": p.rating,
        "review_count": p.review_count,
        "stock": p.stock,
        "rank": r.rank,
        "score": r.score,
        "breakdown": r.breakdown,
        "reasons": r.reasons,
        "tradeoffs": r.tradeoffs,
        "unmet": r.unmet,
        "violations": r.violations,
        "is_alternative": r.is_alternative,
        "key_specs": key_specs(p.category, p.specs),
    }


def _cat(intent: ShoppingIntent, plural: bool = True) -> str:
    label = CATEGORY_LABELS.get(intent.category or "", "products").lower()
    return label if plural else label.rstrip("s")


def _first_clause(reasons: list[str]) -> str:
    useful = [r for r in reasons if not r.startswith("Rated ")][:2]
    return "; ".join(useful) if useful else reasons[0] if reasons else ""


def recommendation_summary(intent: ShoppingIntent, ranked: list[RankedProduct], info: FilterResult) -> str:
    n = len(info.eligible)
    hard = [c.label for c in intent.hard_constraints() if c.label]
    req = f" ({'; '.join(hard)})" if hard else ""
    top = ranked[0]
    parts = [
        f"I found {n} {_cat(intent)} that meet all your requirements{req}."
        if n != 1
        else f"I found 1 {_cat(intent, plural=False)} that meets all your requirements{req}.",
        f"My top pick is **{top.product.name}** at {format_inr(top.product.price_inr)} — {_first_clause(top.reasons)}.",
    ]
    if top.unmet:
        parts.append(f"Trade-off: {top.unmet[0].split(' — ')[0].lower()} is not met.")
    others = ranked[1:3]
    if others:
        parts.append(
            "Also worth a look: "
            + " and ".join(f"**{r.product.name}** ({format_inr(r.product.price_inr)})" for r in others)
            + "."
        )
    return " ".join(parts)


def no_result_summary(intent: ShoppingIntent, info: FilterResult, alternatives: list[RankedProduct]) -> str:
    parts = [f"No in-stock {_cat(intent)} meet all of your requirements."]
    if info.exclusion_counts:
        label, count = max(info.exclusion_counts.items(), key=lambda kv: (kv[1], kv[0]))
        parts.append(f"The biggest blocker is “{label}”, which rules out {count} of {info.considered} {_cat(intent)}.")
    if info.out_of_stock_matches:
        names = ", ".join(p.name for p in info.out_of_stock_matches[:2])
        parts.append(f"{names} would match but {'is' if len(info.out_of_stock_matches) == 1 else 'are'} out of stock.")
    if alternatives:
        parts.append("Here are the closest alternatives — each one is clearly labelled with what it misses.")
    else:
        parts.append("Try relaxing your budget or one of the requirements.")
    return " ".join(parts)


def out_of_stock_notice(info: FilterResult) -> str | None:
    if not info.out_of_stock_matches:
        return None
    names = ", ".join(p.name for p in info.out_of_stock_matches[:3])
    n = len(info.out_of_stock_matches)
    return f"{n} other matching product{'s are' if n > 1 else ' is'} currently out of stock and excluded: {names}."


def polish_with_llm(
    provider: LLMProvider,
    draft: str,
    shown: list[RankedProduct],
    catalog_names: list[str],
    intent: ShoppingIntent,
    *,
    deadline: float | None,
) -> tuple[str, str]:
    """Returns (text, status). Falls back to the draft if the model output is not grounded."""
    if not provider.is_llm:
        return draft, "skipped"
    facts = [
        {
            "name": r.product.name,
            "price": format_inr(r.product.price_inr),
            "rating": r.product.rating,
            "reasons": r.reasons,
            "tradeoffs": r.tradeoffs + r.unmet + r.violations,
        }
        for r in shown
    ]
    # Product descriptions are deliberately NOT passed to the model (they are untrusted text).
    user = f"FACTS:\n{json.dumps(facts, ensure_ascii=False)}\n\nDRAFT:\n{draft}"
    try:
        text = provider.complete_text(POLISH_SYSTEM, user, deadline=deadline).strip()
    except ProviderError:
        return draft, "provider_error"
    if not text:
        return draft, "empty"
    extra = [a for a in (intent.budget_max, intent.budget_min) if a]
    problems = check_grounding(
        text,
        build_facts([(r.product.name, r.product.price_inr, r.product.rating) for r in shown], catalog_names, extra),
    )
    if problems:
        return draft, "rejected_ungrounded: " + "; ".join(problems[:3])
    return text[:1200], "accepted"


def comparison_summary(products: list[ProductSnapshot], table: dict[str, Any]) -> str:
    leads: dict[int, list[str]] = {i: [] for i in range(len(products))}
    for row in table["rows"]:
        if len(row["best"]) == 1:
            leads[row["best"][0]].append(row["label"].lower())
    names = " vs ".join(f"**{p.name}**" for p in products)
    parts = [f"Here's a side-by-side comparison of {names}."]
    for i, p in enumerate(products):
        if leads[i]:
            parts.append(f"{p.name} leads on {', '.join(leads[i][:4])}.")
    oos = [p.name for p in products if p.stock <= 0]
    if oos:
        parts.append(f"Note: {', '.join(oos)} {'is' if len(oos) == 1 else 'are'} currently out of stock.")
    return " ".join(parts)
