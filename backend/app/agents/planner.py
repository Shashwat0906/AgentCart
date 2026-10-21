"""Planner contracts for the Phase 3 agent loop, plus the default deterministic planner.

A planner looks at the task's goal, its deterministic intent and what the tools have returned so far,
and makes **one typed decision** per iteration:

* `ToolCall`: call one registered tool with arguments (the registry validates them again);
* `Final`: stop, naming products that tool outputs proved eligible;
* `Clarify`: stop and ask the user a question.

Rules that hold for every planner (the rule-based one here, and the optional LLM planner later):

* Decisions are data, validated against strict schemas. Nothing a planner returns is executed as code.
* Hard constraints and the budget come from the user's input via the deterministic parser
  (`derive_intent`), never from a planner. Tools that check eligibility read them from the task,
  not from planner arguments.
* `check_decision` rejects tools that weren't offered, and final answers or proposals naming products
  that a constraint check hasn't passed.

This module is pure: no database, network or provider access. The loop runner (S3) executes decisions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from app.agents.guardrails import sanitize_user_text
from app.agents.intent import ShoppingIntent
from app.agents.parser import RequestParser, quantity_mentions
from app.agents.validation import validate_intent
from app.services.specs import CATEGORY_LABELS

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{2,40}$"
MAX_FINAL_PRODUCTS = 4
SEARCH_PAGE_SIZE = 24  # a category holds at most 25 products: evaluate the whole shortlist


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# ------------------------------------------------------------------ decisions
class ToolCall(_Strict):
    kind: Literal["tool"] = "tool"
    tool: str = Field(pattern=TOOL_NAME_PATTERN)
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=300)


class Final(_Strict):
    kind: Literal["final"] = "final"
    product_ids: list[int] = Field(default_factory=list, max_length=MAX_FINAL_PRODUCTS)
    notes: str = Field(default="", max_length=500)

    @field_validator("product_ids")
    @classmethod
    def _positive_unique(cls, v: list[int]) -> list[int]:
        if any(i < 1 for i in v) or len(set(v)) != len(v):
            raise ValueError("product ids must be positive and unique")
        return v


class Clarify(_Strict):
    kind: Literal["clarify"] = "clarify"
    question: str = Field(min_length=1, max_length=300)


PlannerDecision = Annotated[ToolCall | Final | Clarify, Field(discriminator="kind")]
_DECISION = TypeAdapter(PlannerDecision)


class InvalidDecision(Exception):
    """A planner returned something that isn't a valid decision. Carries locations and messages only."""

    def __init__(self, code: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.errors = errors or []


def parse_decision(raw: Any) -> ToolCall | Final | Clarify:
    """Validate untrusted planner output (e.g. model JSON). Never echoes the input back."""
    if not isinstance(raw, Mapping):
        raise InvalidDecision("invalid_decision", [{"loc": [], "msg": "decision must be an object"}])
    try:
        return _DECISION.validate_python(dict(raw))
    except ValidationError as exc:
        errors = [
            {"loc": [str(p) for p in e["loc"]], "msg": e["msg"]}
            for e in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        raise InvalidDecision("invalid_decision", errors) from exc


# ------------------------------------------------------------------ the task's goal and intent
class AgentGoal(_Strict):
    """Input of a `shopping_agent` task. Structured fields can only tighten what the text says."""

    goal: str = Field(min_length=3, max_length=500)
    budget_max_inr: int | None = Field(default=None, ge=1, le=10_000_000)
    category: str | None = None
    allow_proposals: bool = False  # the user opts in to pending cart proposals (never executed here)

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str | None) -> str | None:
        if v is not None and v not in CATEGORY_LABELS:
            raise ValueError("unknown category")
        return v


def derive_intent(goal: AgentGoal, *, max_item_quantity: int) -> tuple[ShoppingIntent, list[dict[str, Any]]]:
    """Deterministic intent (category, budget, hard constraints) and any blocking questions.

    Uses the same sanitising, parsing and validation as the chat agent. An explicit budget can only
    lower the budget found in the text; an explicit category replaces the parsed one. A cart quantity above
    `max_item_quantity` (when the task may propose) is a blocking question, asked before any research.
    """
    intent = RequestParser().parse(sanitize_user_text(goal.goal))
    if goal.category is not None:
        intent = intent.model_copy(update={"category": goal.category})
    if goal.budget_max_inr is not None:
        cap = goal.budget_max_inr if intent.budget_max is None else min(intent.budget_max, goal.budget_max_inr)
        intent = intent.model_copy(update={"budget_max": cap})
    result = validate_intent(intent, max_item_quantity=max_item_quantity)
    questions = [dict(q) for q in result.questions]
    it = result.intent
    if goal.allow_proposals and it.wants_cart_add:
        it, question = _cart_quantity(it, goal.goal, max_item_quantity)
        if question is not None:  # asked before any research; a quantity is never capped or moved silently
            questions.append(question)
    return it, questions


def _cart_quantity(
    intent: ShoppingIntent, text: str, max_item_quantity: int
) -> tuple[ShoppingIntent, dict[str, Any] | None]:
    """The quantity to propose, taken only from a quantity the user attached to the task's own product (or to
    no product: "add 2 to my cart"), or a clarifying question when that isn't clear or is over the limit."""
    wanted = CATEGORY_LABELS[intent.category].lower() if intent.category else "products"
    mentions = quantity_mentions(sanitize_user_text(text))

    def ask(qid: str, question: str) -> tuple[ShoppingIntent, dict[str, Any]]:
        return intent, {"id": qid, "question": question[:300], "options": []}

    named = {(n, cat) for n, cat in mentions}
    if len({cat for _, cat in named if cat}) > 1 or len({n for n, _ in named}) > 1:
        listed = ", ".join(f"{n} {CATEGORY_LABELS[c].lower() if c else 'units'}" for n, c in mentions)
        return ask(
            "quantity_ambiguous",
            f"Your request mentions more than one quantity ({listed}). This task can propose one product: "
            f"how many {wanted} would you like to add (1–{max_item_quantity})?",
        )
    other = next(((n, c) for n, c in mentions if c and c != intent.category), None)
    if other is not None:
        return ask(
            "quantity_product",
            f"You asked for {other[0]} {CATEGORY_LABELS[other[1]].lower()}, but this task is looking for {wanted}. "
            f"How many {wanted} would you like to add (1–{max_item_quantity})?",
        )
    if mentions:
        quantity = mentions[0][0]
    elif intent.cart_quantity != 1:  # a number the parser read but can't tie to a product: don't guess
        return ask("quantity_ambiguous", f"How many {wanted} would you like to add (1–{max_item_quantity})?")
    else:
        quantity = 1
    if quantity > max_item_quantity:
        return ask(
            "quantity",
            f"You asked for {quantity}, but at most {max_item_quantity} units of one product can be added to a "
            f"cart. How many would you like (1–{max_item_quantity})?",
        )
    return intent.model_copy(update={"cart_quantity": quantity}), None


# ------------------------------------------------------------------ state and the constraint-check contract
class ConstraintResult(_Strict):
    product_id: int
    passes: bool
    violations: list[str] = Field(default_factory=list)


class ConstraintReport(_Strict):
    """Output of the `evaluate_constraints` tool (S2): one result per requested product, in order.

    `constraints` names every requirement that was checked (from the task, never from tool arguments)."""

    results: list[ConstraintResult]
    constraints: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Observation:
    """What one executed decision returned. `data` is the tool's validated output."""

    tool: str
    args: dict[str, Any]
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class PlannerState:
    goal: AgentGoal
    intent: ShoppingIntent
    questions: list[dict[str, Any]] = field(default_factory=list)
    observations: tuple[Observation, ...] = ()

    def latest(self, tool: str) -> Observation | None:
        """The most recent successful observation of `tool`."""
        return next((o for o in reversed(self.observations) if o.tool == tool and o.ok), None)

    def failed(self, tool: str) -> Observation | None:
        """The most recent failure of `tool` if it never succeeded afterwards."""
        for o in reversed(self.observations):
            if o.tool == tool:
                return None if o.ok else o
        return None

    def eligible_ids(self) -> list[int] | None:
        """Products the latest constraint check passed, in check order; None if nothing was checked."""
        obs = self.latest("evaluate_constraints")
        if obs is None or obs.data is None:
            return None
        report = ConstraintReport.model_validate(obs.data)
        return [r.product_id for r in report.results if r.passes]


class Planner(Protocol):
    name: str

    def decide(self, state: PlannerState, tools: Sequence[Mapping[str, Any]]) -> ToolCall | Final | Clarify: ...


def tool_names(tools: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    return frozenset(str(t["name"]) for t in tools)


def check_decision(
    decision: ToolCall | Final | Clarify, state: PlannerState, tools: Sequence[Mapping[str, Any]]
) -> str | None:
    """Server-side guard applied to every decision before it is acted on. Returns an error code or None."""
    if isinstance(decision, ToolCall):
        if decision.tool not in tool_names(tools):
            return "unknown_tool"
        if decision.tool == "propose_add_to_cart":
            eligible = state.eligible_ids() or []
            if decision.args.get("product_id") not in eligible:
                return "ungrounded_proposal"
            # The quantity is the user's, read deterministically from the goal; a planner can't choose it.
            if decision.args.get("quantity", 1) != state.intent.cart_quantity:
                return "invented_quantity"
        return None
    if isinstance(decision, Final):
        allowed = set(state.eligible_ids() or [])
        if any(pid not in allowed for pid in decision.product_ids):
            return "ungrounded_final"
    return None


# ------------------------------------------------------------------ the default planner
class RuleBasedPlanner:
    """Deterministic, offline policy:
    search → check constraints → compare (2+ eligible) → read cart → propose (only if asked and
    allowed) → final. It needs no provider, makes the same decision for the same state, and only
    uses the tools it is offered."""

    name = "rule_based"

    def decide(self, state: PlannerState, tools: Sequence[Mapping[str, Any]]) -> ToolCall | Final | Clarify:
        names = tool_names(tools)
        intent = state.intent
        if state.questions:
            return Clarify(question=str(state.questions[0].get("question", "What are you shopping for?"))[:300])
        if intent.category is None:
            return Clarify(question="What kind of product are you shopping for?")
        label = CATEGORY_LABELS[intent.category].lower()

        failed = next((o for o in state.observations if not o.ok and state.failed(o.tool) is o), None)
        if failed is not None:
            return Final(notes=f"Stopped: the {failed.tool} step failed ({failed.error_code or 'error'}).")

        search = state.latest("search_catalog")
        if search is None:
            if "search_catalog" not in names:
                return Final(notes="Catalogue search isn't available, so nothing can be recommended.")
            return ToolCall(
                tool="search_catalog", args=_search_args(intent), rationale=f"Find in-stock {label} within the budget"
            )
        items = list((search.data or {}).get("items") or [])
        if not items:
            return Final(notes=f"No in-stock {label} match the search.")

        eligible = state.eligible_ids()
        if eligible is None:
            if "evaluate_constraints" not in names:
                return Final(notes="The requirements can't be verified, so nothing is recommended.")
            return ToolCall(
                tool="evaluate_constraints",
                args={"product_ids": [int(p["id"]) for p in items]},
                rationale=f"Check all {len(items)} candidates against every hard requirement",
            )
        if not eligible:
            return Final(notes=f"None of the {len(items)} candidates meets every requirement.")

        ranked = _order(eligible, items, intent)
        if len(ranked) >= 2 and "compare_products" in names and state.latest("compare_products") is None:
            return ToolCall(
                tool="compare_products",
                args={"product_ids": ranked[:3]},
                rationale=f"Compare the top {min(3, len(ranked))} eligible {label}",
            )
        if "read_cart" in names and state.latest("read_cart") is None and state.failed("read_cart") is None:
            return ToolCall(tool="read_cart", rationale="Include what is already in the cart")
        wants_proposal = intent.wants_cart_add and state.goal.allow_proposals
        if wants_proposal and "propose_add_to_cart" in names and state.latest("propose_add_to_cart") is None:
            return ToolCall(
                tool="propose_add_to_cart",
                args={"product_id": ranked[0], "quantity": intent.cart_quantity},
                rationale="Propose the top eligible product for your approval (nothing is added until you approve)",
            )
        notes = f"{len(eligible)} of {len(items)} candidates meet every requirement."
        if intent.wants_cart_add and not state.goal.allow_proposals:
            notes += " Cart proposals weren't enabled for this task, so nothing was proposed."
        return Final(product_ids=ranked[:3], notes=notes)


def _search_args(intent: ShoppingIntent) -> dict[str, Any]:
    args: dict[str, Any] = {
        "category": intent.category,
        "in_stock": True,
        "sort": "price_asc" if intent.sort_hint == "cheapest" else "rating",
        "page_size": SEARCH_PAGE_SIZE,
    }
    if intent.budget_max is not None:
        args["max_price_inr"] = intent.budget_max
    if intent.budget_min is not None:
        args["min_price_inr"] = intent.budget_min
    return args


def _order(eligible: list[int], items: list[dict[str, Any]], intent: ShoppingIntent) -> list[int]:
    """Eligible ids in a deterministic order: cheapest first when asked, otherwise the search order
    (best rated first); ties broken by id."""
    by_id = {int(p["id"]): p for p in items}
    known = [i for i in eligible if i in by_id]
    if intent.sort_hint == "cheapest":
        return sorted(known, key=lambda i: (int(by_id[i]["price_inr"]), i))
    position = {int(p["id"]): n for n, p in enumerate(items)}
    return sorted(known, key=lambda i: (position[i], i))
