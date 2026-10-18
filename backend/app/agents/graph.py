"""The AgentCart shopping workflow as an explicit LangGraph state machine.

    parse_request → extract_constraints → validate_constraints ─┬─ clarify ───────────────────────────┐
                                                                ├─ search_catalog → rank_products     │
                                                                │      → explain_recommendations ─┐   │
                                                                ├─ resolve_reference ─────────────┤   │
                                                                ├─ respond_simple ────────────────┤   │
                                                                └─ compare_products ──────────────┼─→ END
                                                         propose_actions → await_approval ←───────┘
    Any node failure → safe_failure → END.

The graph never mutates carts, orders or preferences. It only *proposes* actions; the API
persists them as `pending` and executes them only after the owner approves (re-validating
price and stock at that moment). Checkout performs a second, final revalidation.
"""

from __future__ import annotations

import logging
import operator
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agents import explain
from app.agents.guardrails import SECURITY_NOTICE, detect_injection, sanitize_user_text
from app.agents.intent import ShoppingIntent
from app.agents.llm_extract import enrich_with_llm
from app.agents.memory import apply_saved_preferences, merge_with_context
from app.agents.parser import RequestParser, describe_intent
from app.agents.tools import ToolBox
from app.agents.validation import validate_intent
from app.providers.base import LLMProvider, ProviderError
from app.services.catalog import ProductSnapshot, comparison_table
from app.services.preferences import sensitive_reason
from app.services.ranking import FilterResult, RankedProduct, filter_products, find_alternatives, rank_products
from app.services.specs import CATEGORY_LABELS, format_inr

logger = logging.getLogger("agentcart.agent")
_parser = RequestParser()
TOP_K = 5


@dataclass
class AgentDeps:
    tools: ToolBox
    provider: LLMProvider
    max_item_quantity: int = 10
    deadline_seconds: float = 45.0
    started: float = field(default_factory=time.monotonic)

    @property
    def deadline(self) -> float:
        return self.started + self.deadline_seconds

    def past_deadline(self) -> bool:
        return time.monotonic() >= self.deadline


class AgentState(TypedDict, total=False):
    # inputs
    message: str
    context: dict[str, Any]
    saved_preferences: list[dict[str, Any]]
    weights: dict[str, float] | None
    # working memory
    text: str
    security_flags: list[str]
    intent: ShoppingIntent
    understood: list[str]
    applied_preferences: list[str]
    issues: list[str]
    questions: list[dict[str, Any]]
    soft_questions: list[dict[str, Any]]
    route: str
    candidates: list[ProductSnapshot]
    filter_info: FilterResult
    ranked: list[RankedProduct]
    alternatives: list[RankedProduct]
    comparison: dict[str, Any] | None
    target: ProductSnapshot | None
    proposed_actions: list[dict[str, Any]]
    notices: Annotated[list[str], operator.add]
    response_type: str
    summary: str
    awaiting_approval: bool
    llm_used: bool
    failed: bool
    error: str
    trace: Annotated[list[dict[str, Any]], operator.add]


NodeFn = Callable[[AgentState, AgentDeps], dict[str, Any]]


def node(name: str) -> Callable[[NodeFn], Callable[[AgentState, RunnableConfig], dict[str, Any]]]:
    """Wrap a node with timing, tracing and safe failure handling."""

    def deco(fn: NodeFn) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
        def wrapped(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            deps: AgentDeps = config["configurable"]["deps"]
            t0 = time.perf_counter()
            try:
                update = fn(state, deps) or {}
                detail = update.pop("_detail", "")
                status = "ok"
            except Exception as exc:
                logger.exception("agent node %s failed", name)
                update = {"failed": True, "error": f"{name}: {type(exc).__name__}"}
                detail, status = type(exc).__name__, "error"
            update["trace"] = [
                {"node": name, "status": status, "ms": round((time.perf_counter() - t0) * 1000, 1), "detail": detail}
            ]
            return update

        wrapped.__name__ = name
        return wrapped

    return deco


# =========================================================================== nodes


@node("parse_request")
def parse_request(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    text = sanitize_user_text(state.get("message", ""))
    flags = detect_injection(text)
    out: dict[str, Any] = {"text": text, "security_flags": flags, "notices": [SECURITY_NOTICE] if flags else []}
    out["_detail"] = f"{len(text)} chars" + (f", flags={flags}" if flags else "")
    return out


@node("extract_constraints")
def extract_constraints(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    text = state["text"]
    intent = _parser.parse(text)
    details = ["rules"]
    llm_used = False
    if deps.provider.is_llm and intent.kind == "search" and not deps.past_deadline():
        try:
            intent, notes = enrich_with_llm(deps.provider, text, intent, deadline=deps.deadline)
            llm_used = True
            details.append(f"{deps.provider.name}({len(notes)} additions)")
        except (ProviderError, ValueError) as exc:
            details.append(f"{deps.provider.name} unavailable → rules only ({type(exc).__name__})")
    intent = merge_with_context(intent, state.get("context") or {}, text)
    if intent.inherited_from_context:
        details.append("merged with conversation context")
    intent, applied = apply_saved_preferences(intent, state.get("saved_preferences") or [])
    return {"intent": intent, "applied_preferences": applied, "llm_used": llm_used, "_detail": ", ".join(details)}


@node("validate_constraints")
def validate_constraints(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    res = validate_intent(state["intent"], max_item_quantity=deps.max_item_quantity)
    intent = res.intent
    if res.questions:
        route = "clarify"
    elif intent.kind == "compare":
        route = "compare_products"
    elif intent.kind == "cart_add_reference":
        route = "resolve_reference"
    elif intent.kind in {"help", "reset", "save_preference"}:
        route = "respond_simple"
    else:
        route = "search_catalog"
    return {
        "intent": intent,
        "issues": res.issues,
        "questions": res.questions,
        "soft_questions": res.soft_questions,
        "understood": describe_intent(intent),
        "route": route,
        "_detail": f"route={route}, {len(intent.constraints)} constraints, {len(res.issues)} issues",
    }


@node("clarify")
def clarify(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    qs = state.get("questions") or []
    lead = "I need one detail before I search." if len(qs) == 1 else "I need a couple of details before I search."
    if state.get("security_flags") and not state["intent"].category:
        lead = "I can help you shop, but I can't change prices, skip approvals or check out on my own."
    return {
        "response_type": "clarification",
        "summary": f"{lead} {qs[0]['question'] if qs else ''}".strip(),
        "_detail": f"{len(qs)} question(s)",
    }


@node("search_catalog")
def search_catalog(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    intent = state["intent"]
    assert intent.category is not None
    candidates = deps.tools.search_catalog(intent.category)
    return {"candidates": candidates, "_detail": f"{len(candidates)} {intent.category} retrieved"}


@node("rank_products")
def rank_products_node(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    intent = state["intent"]
    candidates = state.get("candidates") or []
    info = filter_products(candidates, intent)
    ranked = rank_products(info.eligible, intent, state.get("weights"), limit=TOP_K)
    alternatives = [] if ranked else find_alternatives(candidates, intent, state.get("weights"))
    return {
        "filter_info": info,
        "ranked": ranked,
        "alternatives": alternatives,
        "_detail": f"{len(info.eligible)} eligible / {info.considered} considered, "
        f"{len(info.out_of_stock_matches)} out of stock, {len(alternatives)} alternatives",
    }


@node("explain_recommendations")
def explain_recommendations(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    intent, info = state["intent"], state["filter_info"]
    ranked, alternatives = state.get("ranked") or [], state.get("alternatives") or []
    notices: list[str] = []
    if ranked:
        draft = explain.recommendation_summary(intent, ranked, info)
        response_type = "recommendations"
        shown = ranked
        oos = explain.out_of_stock_notice(info)
        if oos:
            notices.append(oos)
        comparison = comparison_table([r.product for r in ranked[:3]]) if len(ranked) >= 2 else None
    else:
        draft = explain.no_result_summary(intent, info, alternatives)
        response_type = "no_results"
        shown = alternatives
        comparison = None
    status = "skipped"
    summary = draft
    if deps.provider.is_llm and shown and not deps.past_deadline():
        names = deps.tools.catalog_names()
        summary, status = explain.polish_with_llm(deps.provider, draft, shown, names, intent, deadline=deps.deadline)
    return {
        "summary": summary,
        "response_type": response_type,
        "comparison": comparison,
        "notices": notices,
        "_detail": f"llm_polish={status}",
    }


@node("compare_products")
def compare_products(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    text = state["text"].lower()
    ctx_ids: list[int] = list((state.get("context") or {}).get("last_recommendation_ids") or [])
    products: list[ProductSnapshot] = []

    ref = re.search(r"\b(first|top)\s+(two|2|three|3)\b|\b(them|these|those|all|both)\b", text)
    if ref and ctx_ids:
        n = 3 if (ref.group(2) in {"three", "3"} or ref.group(3) in {"them", "these", "those", "all"}) else 2
        products = deps.tools.get_products(ctx_ids[:n])
    if len(products) < 2:
        products = deps.tools.find_products_by_name(state["text"])
    if len(products) < 2:
        return {
            "response_type": "clarification",
            "questions": [
                {
                    "id": "compare",
                    "question": "Which products should I compare? Name at least two (for example “Sony WH-1000XM5 vs Bose QuietComfort Ultra”) or search first and say “compare the top three”.",
                    "options": [],
                }
            ],
            "summary": "I couldn't tell which products to compare.",
            "_detail": "insufficient products",
        }
    table = comparison_table(products)
    return {
        "response_type": "comparison",
        "comparison": table,
        "summary": explain.comparison_summary(products, table),
        "ranked": rank_products(
            products, state["intent"].model_copy(update={"category": products[0].category}), state.get("weights")
        ),
        "_detail": f"{len(products)} products",
    }


@node("resolve_reference")
def resolve_reference(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    intent = state["intent"]
    ids: list[int] = list((state.get("context") or {}).get("last_recommendation_ids") or [])
    if not ids:
        return {
            "response_type": "clarification",
            "summary": "Which product would you like to add? Ask me to find something first, then say “add the top pick to my cart”.",
            "questions": [
                {"id": "reference", "question": "What are you shopping for?", "options": list(CATEGORY_LABELS.values())}
            ],
            "_detail": "no previous recommendations",
        }
    products = deps.tools.get_products(ids)
    if "__cheapest_ref__" in intent.keywords and products:
        target = min(products, key=lambda p: (p.price_inr, p.id))
    else:
        idx = intent.reference_index or 0
        if idx >= len(products):
            return {
                "response_type": "clarification",
                "summary": f"I only showed {len(products)} option{'s' if len(products) != 1 else ''}. Which one should I add?",
                "questions": [{"id": "reference", "question": "Which option?", "options": [p.name for p in products]}],
                "_detail": f"index {idx} out of range",
            }
        target = products[idx]
    return {"target": target, "response_type": "cart_proposal", "summary": "", "_detail": f"→ {target.sku}"}


@node("respond_simple")
def respond_simple(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    kind = state["intent"].kind
    if kind == "reset":
        return {
            "response_type": "reset",
            "summary": "Done — I've cleared this conversation's context. Your saved preferences are unchanged. What would you like to shop for?",
        }
    if kind == "save_preference":
        return {"response_type": "preference_proposal", "summary": ""}
    return {
        "response_type": "help",
        "summary": (
            "Hi! Tell me what you need in plain words — for example “a laptop under ₹70,000 for machine learning with 16 GB RAM” "
            "or “noise-cancelling earbuds under 10k”. I'll search the catalogue, rank options against your requirements, "
            "explain the trade-offs and, if you like, propose cart changes for you to approve."
        ),
    }


@node("propose_actions")
def propose_actions(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    intent = state["intent"]
    actions: list[dict[str, Any]] = []
    notices: list[str] = []
    summary = state.get("summary", "")
    target: ProductSnapshot | None = state.get("target")
    if (
        target is None
        and intent.wants_cart_add
        and state.get("ranked")
        and state.get("response_type") == "recommendations"
    ):
        ranked = state["ranked"]
        idx = intent.reference_index or 0
        target = ranked[idx].product if idx < len(ranked) else ranked[0].product  # type: ignore[assignment]
    if target is not None:
        qty = intent.cart_quantity
        if "__invalid_quantity__" in intent.keywords:
            notices.append("Quantity must be a whole number of at least 1, so I didn't propose a cart change.")
        elif qty > deps.max_item_quantity:
            notices.append(
                f"I can't add {qty} units — the limit is {deps.max_item_quantity} per product. Ask again with a smaller quantity."
            )
        elif target.stock <= 0:
            notices.append(f"{target.name} is out of stock, so I can't add it to your cart.")
        elif qty > target.stock:
            notices.append(
                f"Only {target.stock} unit{'s' if target.stock != 1 else ''} of {target.name} {'are' if target.stock != 1 else 'is'} in stock."
            )
        else:
            actions.append(
                {
                    "kind": "add_to_cart",
                    "payload": {
                        "product_id": target.id,
                        "quantity": qty,
                        "expected_price_inr": target.price_inr,
                        "product_name": target.name,
                    },
                    "summary": f"Add {qty} × {target.name} ({format_inr(target.price_inr)} each) to your cart",
                }
            )
        if state.get("response_type") == "cart_proposal":
            summary = (
                f"I can add {qty} × **{target.name}** at {format_inr(target.price_inr)} each. Please approve or reject below."
                if actions
                else "I couldn't propose that cart change."
            )
    pref = intent.preference_to_save
    if pref is not None and state.get("security_flags"):
        notices.append(
            "I didn't offer to save a preference because the message contained instructions aimed at the assistant."
        )
        pref = None
    if pref is not None:
        reason = sensitive_reason(pref.value)
        if reason:
            notices.append(f"I won't save that as a preference because it looks like {reason}.")
        else:
            label = {
                "preferred_brand": f"Prefer {pref.value}",
                "avoided_brand": f"Avoid {pref.value}",
                "budget_max": f"Budget for {CATEGORY_LABELS.get(pref.category or '', pref.category)}: ≤ {format_inr(int(pref.value))}"
                if pref.value.isdigit()
                else pref.value,
                "use_case": f"Usually shopping for {pref.value}",
                "note": f"Note: {pref.value}",
            }[pref.kind]
            actions.append({"kind": "save_preference", "payload": pref.model_dump(), "summary": f"Remember: {label}"})
            if state.get("response_type") == "preference_proposal":
                summary = f"Want me to remember this for future searches? “{label}”. Approve below to save it — you can edit or delete it anytime under Preferences."
    if state.get("response_type") == "preference_proposal" and not actions:
        summary = "I didn't save anything."
    return {"proposed_actions": actions, "notices": notices, "summary": summary, "_detail": f"{len(actions)} proposed"}


@node("await_approval")
def await_approval(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    pending = bool(state.get("proposed_actions"))
    notices = ["Nothing has changed yet — approve or reject the proposed action below."] if pending else []
    return {
        "awaiting_approval": pending,
        "notices": notices,
        "_detail": "waiting for user" if pending else "nothing to approve",
    }


@node("safe_failure")
def safe_failure(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    return {
        "response_type": "error",
        "proposed_actions": [],
        "awaiting_approval": False,
        "ranked": [],
        "alternatives": [],
        "summary": "Sorry — something went wrong while I was working on that, so I stopped without changing anything. Please try again or rephrase your request.",
        "_detail": state.get("error", ""),
    }


# =========================================================================== wiring


def _guard(target: str) -> Callable[[AgentState], str]:
    def route(state: AgentState) -> str:
        return "safe_failure" if state.get("failed") else target

    return route


def _after_validate(state: AgentState) -> str:
    return "safe_failure" if state.get("failed") else state["route"]


def _after_branch(state: AgentState) -> str:
    if state.get("failed"):
        return "safe_failure"
    return END if state.get("response_type") == "clarification" else "propose_actions"


def build_graph() -> Any:
    g = StateGraph(AgentState)
    for name, fn in [
        ("parse_request", parse_request),
        ("extract_constraints", extract_constraints),
        ("validate_constraints", validate_constraints),
        ("clarify", clarify),
        ("search_catalog", search_catalog),
        ("rank_products", rank_products_node),
        ("explain_recommendations", explain_recommendations),
        ("compare_products", compare_products),
        ("resolve_reference", resolve_reference),
        ("respond_simple", respond_simple),
        ("propose_actions", propose_actions),
        ("await_approval", await_approval),
        ("safe_failure", safe_failure),
    ]:
        g.add_node(name, fn)  # type: ignore[call-overload]  # LangGraph can't infer our wrapped signature
    branches = ["clarify", "search_catalog", "compare_products", "resolve_reference", "respond_simple", "safe_failure"]
    g.add_edge(START, "parse_request")
    g.add_conditional_edges("parse_request", _guard("extract_constraints"), ["extract_constraints", "safe_failure"])
    g.add_conditional_edges(
        "extract_constraints", _guard("validate_constraints"), ["validate_constraints", "safe_failure"]
    )
    g.add_conditional_edges("validate_constraints", _after_validate, branches)
    g.add_conditional_edges("search_catalog", _guard("rank_products"), ["rank_products", "safe_failure"])
    g.add_conditional_edges(
        "rank_products", _guard("explain_recommendations"), ["explain_recommendations", "safe_failure"]
    )
    g.add_conditional_edges("explain_recommendations", _guard("propose_actions"), ["propose_actions", "safe_failure"])
    g.add_conditional_edges("compare_products", _after_branch, ["propose_actions", "safe_failure", END])
    g.add_conditional_edges("resolve_reference", _after_branch, ["propose_actions", "safe_failure", END])
    g.add_conditional_edges("respond_simple", _guard("propose_actions"), ["propose_actions", "safe_failure"])
    g.add_conditional_edges("propose_actions", _guard("await_approval"), ["await_approval", "safe_failure"])
    g.add_edge("clarify", END)
    g.add_edge("await_approval", END)
    g.add_edge("safe_failure", END)
    return g.compile()


_GRAPH = None


def get_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


# =========================================================================== entry point


@dataclass
class AgentResult:
    response_type: str
    summary: str
    intent: ShoppingIntent | None
    understood: list[str]
    recommendations: list[dict[str, Any]]
    alternatives: list[dict[str, Any]]
    comparison: dict[str, Any] | None
    questions: list[dict[str, Any]]
    notices: list[str]
    applied_preferences: list[str]
    proposed_actions: list[dict[str, Any]]
    awaiting_approval: bool
    security_flags: list[str]
    trace: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    provider: str
    llm_used: bool
    weights: dict[str, float]

    def payload(self) -> dict[str, Any]:
        return {
            "type": self.response_type,
            "understood": self.understood,
            "recommendations": self.recommendations,
            "alternatives": self.alternatives,
            "comparison": self.comparison,
            "questions": self.questions,
            "notices": self.notices,
            "applied_preferences": self.applied_preferences,
            "security_flags": self.security_flags,
            "trace": self.trace,
            "tool_calls": self.tool_calls,
            "provider": self.provider,
            "llm_used": self.llm_used,
            "weights": self.weights,
            "awaiting_approval": self.awaiting_approval,
        }


def run_agent(
    message: str,
    deps: AgentDeps,
    *,
    context: dict[str, Any] | None = None,
    saved_preferences: list[dict[str, Any]] | None = None,
    weights: dict[str, float] | None = None,
) -> AgentResult:
    from app.services.ranking import normalise_weights

    deps.started = time.monotonic()
    state: AgentState = get_graph().invoke(
        {
            "message": message,
            "context": context or {},
            "saved_preferences": saved_preferences or [],
            "weights": weights,
            "notices": [],
            "trace": [],
        },
        config={"configurable": {"deps": deps}, "recursion_limit": 25},
    )
    intent = state.get("intent")
    ranked = state.get("ranked") or []
    issues = state.get("issues") or []
    questions = list(state.get("questions") or [])
    if state.get("response_type") == "recommendations":
        questions += state.get("soft_questions") or []
    return AgentResult(
        response_type=state.get("response_type", "error"),
        summary=state.get("summary", ""),
        intent=intent,
        understood=state.get("understood") or [],
        recommendations=[explain.product_card(r) for r in ranked],
        alternatives=[explain.product_card(r) for r in state.get("alternatives") or []],
        comparison=state.get("comparison"),
        questions=questions,
        notices=list(dict.fromkeys(issues + list(state.get("notices") or []))),
        applied_preferences=state.get("applied_preferences") or [],
        proposed_actions=state.get("proposed_actions") or [],
        awaiting_approval=bool(state.get("awaiting_approval")),
        security_flags=state.get("security_flags") or [],
        trace=state.get("trace") or [],
        tool_calls=deps.tools.calls,
        provider=deps.provider.name,
        llm_used=bool(state.get("llm_used")),
        weights=normalise_weights(weights),
    )
