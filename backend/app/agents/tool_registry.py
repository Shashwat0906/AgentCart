"""Typed tool registry for the shopping agent (Phase 1: read-only tools).

Every tool has a strict Pydantic input schema, an output schema (the same response models the HTTP
API uses), a permission level, and an authorization rule. `ToolRegistry.execute` is the only entry
point: it validates input, checks permission and identity, runs the handler, refuses any database
write, validates the output, maps errors to stable codes, and records an audit entry.

Phase 1 registers read-only tools only, and nothing in the agent graph or the API uses this module
yet, so production behaviour is unchanged. Later phases add "propose" tools (pending, approval-gated
actions). There is deliberately no "execute" permission: state changes stay behind user approval.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, NotFoundError, ValidationFailed
from app.models import Cart
from app.schemas import CartOut, ComparisonOut, ProductDetailOut, ProductPage
from app.services import cart as cart_service
from app.services import catalog
from app.services.catalog import SortKey
from app.services.specs import CATEGORY_LABELS

logger = logging.getLogger("agentcart.tools")

Permission = Literal["read"]
REGISTRABLE_PERMISSIONS: frozenset[str] = frozenset({"read"})
ErrorCode = Literal[
    "unknown_tool",
    "forbidden",
    "unauthorized",
    "invalid_input",
    "not_found",
    "invalid_request",
    "write_attempted",
    "invalid_output",
    "internal_error",
]
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


# ------------------------------------------------------------------ input schemas
class ToolInput(BaseModel):
    """Strict: unknown fields are rejected and nothing is coerced ("3" is not 3, 1 is not True)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SearchCatalogInput(ToolInput):
    query: str | None = Field(default=None, min_length=1, max_length=120, description="Keywords")
    category: str | None = Field(default=None, description="One of: " + ", ".join(CATEGORY_LABELS))
    brands: list[str] = Field(default_factory=list, max_length=20)
    min_price_inr: int | None = Field(default=None, ge=0, le=10_000_000)
    max_price_inr: int | None = Field(default=None, ge=0, le=10_000_000)
    min_rating: float | None = Field(default=None, ge=0, le=5)
    in_stock: bool | None = None
    sort: SortKey = "relevance"
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=12, ge=1, le=48)

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str | None) -> str | None:
        if v is not None and v not in CATEGORY_LABELS:
            raise ValueError("unknown category")
        return v

    @field_validator("brands")
    @classmethod
    def _brand_names(cls, v: list[str]) -> list[str]:
        if any(not b.strip() or len(b) > 60 for b in v):
            raise ValueError("brand names must be 1-60 characters")
        return v

    @model_validator(mode="after")
    def _price_range(self) -> SearchCatalogInput:
        if (
            self.min_price_inr is not None
            and self.max_price_inr is not None
            and self.min_price_inr > self.max_price_inr
        ):
            raise ValueError("min_price_inr must not exceed max_price_inr")
        return self


class GetProductInput(ToolInput):
    product_id: int = Field(ge=1)


class CompareProductsInput(ToolInput):
    product_ids: list[int] = Field(min_length=2, max_length=4)

    @field_validator("product_ids")
    @classmethod
    def _positive_unique(cls, v: list[int]) -> list[int]:
        if any(i < 1 for i in v):
            raise ValueError("product ids must be positive")
        if len(set(v)) != len(v):
            raise ValueError("product ids must be unique")
        return v


class ReadCartInput(ToolInput):
    """No arguments: the cart is always the caller's own, taken from the context, never from input."""


# ------------------------------------------------------------------ execution types
@dataclass(frozen=True)
class ToolContext:
    db: Session
    settings: Settings
    user_id: int | None = None  # the authenticated caller; never taken from tool arguments
    allowed_permissions: frozenset[str] = frozenset({"read"})


@dataclass(frozen=True)
class ToolError:
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    ok: bool
    data: dict[str, Any] | None = None
    error: ToolError | None = None
    ms: float = 0.0


@dataclass(frozen=True)
class ToolCallRecord:
    """Audit entry. Records argument names only: values may contain user text."""

    tool: str
    ok: bool
    error_code: str | None
    arg_names: tuple[str, ...]
    user_id: int | None
    ms: float


Handler = Callable[[ToolContext, Any], dict[str, Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[ToolInput]
    output_model: type[BaseModel]
    handler: Handler
    permission: Permission = "read"
    requires_user: bool = False

    def schema(self) -> dict[str, Any]:
        """Provider-neutral description, usable later for LLM tool calling."""
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission,
            "requires_user": self.requires_user,
            "input_schema": self.input_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.calls: list[ToolCallRecord] = []

    def register(self, spec: ToolSpec) -> None:
        if not _TOOL_NAME.match(spec.name):
            raise ValueError(f"invalid tool name: {spec.name!r}")
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        if spec.permission not in REGISTRABLE_PERMISSIONS:
            raise ValueError(f"permission {spec.permission!r} is not allowed in this phase")
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[n].schema() for n in self.names()]

    def execute(self, name: str, args: Mapping[str, Any] | None, ctx: ToolContext) -> ToolResult:
        t0 = time.perf_counter()
        result = self._execute(name, args, ctx)
        ms = round((time.perf_counter() - t0) * 1000, 2)
        arg_names = tuple(sorted(str(k) for k in args)) if isinstance(args, Mapping) else ()
        self.calls.append(
            ToolCallRecord(
                tool=name,
                ok=result.ok,
                error_code=result.error.code if result.error else None,
                arg_names=arg_names,
                user_id=ctx.user_id,
                ms=ms,
            )
        )
        return ToolResult(tool=name, ok=result.ok, data=result.data, error=result.error, ms=ms)

    def _execute(self, name: str, args: Mapping[str, Any] | None, ctx: ToolContext) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            return _fail(name, "unknown_tool", "No such tool")
        if spec.permission not in ctx.allowed_permissions:
            return _fail(name, "forbidden", f"Tool requires {spec.permission!r} permission")
        if spec.requires_user and ctx.user_id is None:
            return _fail(name, "unauthorized", "Sign in required")
        if args is not None and not isinstance(args, Mapping):
            return _fail(name, "invalid_input", "Arguments must be an object")
        try:
            inp = spec.input_model.model_validate(dict(args or {}))
        except ValidationError as exc:
            # Location and message only: never echo the (possibly hostile or large) input back.
            errors = [
                {"loc": [str(p) for p in e["loc"]], "msg": e["msg"]}
                for e in exc.errors(include_url=False, include_input=False, include_context=False)
            ]
            return _fail(name, "invalid_input", "Invalid tool arguments", {"errors": errors})

        try:
            raw = spec.handler(ctx, inp)
        except NotFoundError as exc:
            _rollback_if_dirty(ctx.db)
            return _fail(name, "not_found", exc.message)
        except (ValidationFailed, AppError) as exc:
            _rollback_if_dirty(ctx.db)
            return _fail(name, "invalid_request", exc.message)
        except Exception:
            _rollback_if_dirty(ctx.db)
            logger.exception("tool %s failed", name)
            return _fail(name, "internal_error", "The tool failed unexpectedly")

        if spec.permission == "read" and _rollback_if_dirty(ctx.db):
            logger.error("read-only tool %s modified the session; changes rolled back", name)
            return _fail(name, "write_attempted", "A read-only tool attempted to change data")

        try:
            data = spec.output_model.model_validate(raw).model_dump(mode="json")
        except ValidationError:
            logger.exception("tool %s returned output that does not match its schema", name)
            return _fail(name, "invalid_output", "The tool returned malformed data")
        return ToolResult(tool=name, ok=True, data=data)


def _fail(name: str, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(tool=name, ok=False, error=ToolError(code=code, message=message, details=details or {}))


def _rollback_if_dirty(db: Session) -> bool:
    if db.new or db.dirty or db.deleted:
        db.rollback()
        return True
    return False


# ------------------------------------------------------------------ read-only tools
def _product_out(p: Any) -> dict[str, Any]:
    # Single source of truth for product serialisation: the HTTP API's own helper.
    from app.api.routes_public import product_out

    return product_out(p)


def _search_catalog(ctx: ToolContext, inp: SearchCatalogInput) -> dict[str, Any]:
    items, total = catalog.search_products(
        ctx.db,
        q=inp.query,
        category=inp.category,
        brands=list(inp.brands) or None,
        min_price=inp.min_price_inr,
        max_price=inp.max_price_inr,
        min_rating=inp.min_rating,
        in_stock=inp.in_stock,
        sort=inp.sort,
        page=inp.page,
        page_size=inp.page_size,
    )
    return {
        "items": [_product_out(p) for p in items],
        "total": total,
        "page": inp.page,
        "page_size": inp.page_size,
        "pages": max(1, math.ceil(total / inp.page_size)),
    }


def _get_product(ctx: ToolContext, inp: GetProductInput) -> dict[str, Any]:
    p = catalog.get_product(ctx.db, inp.product_id)
    return {
        **_product_out(p),
        "description": p.description,
        "specs": catalog.all_specs(p.category, p.specs or {}),
        "raw_specs": p.specs or {},
    }


def _compare_products(ctx: ToolContext, inp: CompareProductsInput) -> dict[str, Any]:
    products = catalog.products_by_ids(ctx.db, list(inp.product_ids))
    if len(products) != len(inp.product_ids):
        raise NotFoundError("One or more products do not exist")
    return {**catalog.comparison_table(products), "products": [_product_out(p) for p in products]}


def _read_cart(ctx: ToolContext, inp: ReadCartInput) -> dict[str, Any]:
    assert ctx.user_id is not None  # guaranteed by requires_user
    cart = ctx.db.scalar(select(Cart).where(Cart.user_id == ctx.user_id))
    # Unlike GET /api/cart this never creates a cart row: an unsaved empty Cart is only a view model.
    return cart_service.cart_view(cart if cart is not None else Cart(user_id=ctx.user_id), ctx.settings)


READ_ONLY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_catalog",
        description="Search the product catalogue by keywords, category, brand, price, rating and stock.",
        input_model=SearchCatalogInput,
        output_model=ProductPage,
        handler=_search_catalog,
    ),
    ToolSpec(
        name="get_product",
        description="Get one product with its description and full specifications.",
        input_model=GetProductInput,
        output_model=ProductDetailOut,
        handler=_get_product,
    ),
    ToolSpec(
        name="compare_products",
        description="Compare 2 to 4 products side by side on price, rating, stock and specifications.",
        input_model=CompareProductsInput,
        output_model=ComparisonOut,
        handler=_compare_products,
    ),
    ToolSpec(
        name="read_cart",
        description="Read the signed-in user's own cart, with totals and any stock problems.",
        input_model=ReadCartInput,
        output_model=CartOut,
        handler=_read_cart,
        requires_user=True,
    ),
)


def build_read_only_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for spec in READ_ONLY_TOOLS:
        registry.register(spec)
    return registry
