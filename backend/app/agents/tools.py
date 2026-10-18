"""Backend tools the agent may call. Every call is recorded for the trace and the
tool-call success metric. The agent cannot call anything that is not defined here."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.services import catalog
from app.services.catalog import ProductSnapshot

T = TypeVar("T")
MAX_TOOL_ATTEMPTS = 2


class CatalogPort(Protocol):
    def products_in_category(self, category: str) -> list[ProductSnapshot]: ...
    def products_by_ids(self, ids: list[int]) -> list[ProductSnapshot]: ...
    def find_by_name(self, text: str) -> list[ProductSnapshot]: ...
    def all_names(self) -> list[str]: ...


class DbCatalog:
    def __init__(self, db: Session) -> None:
        self.db = db

    def products_in_category(self, category: str) -> list[ProductSnapshot]:
        return [ProductSnapshot.of(p) for p in catalog.products_in_category(self.db, category)]

    def products_by_ids(self, ids: list[int]) -> list[ProductSnapshot]:
        return [ProductSnapshot.of(p) for p in catalog.products_by_ids(self.db, ids)]

    def find_by_name(self, text: str) -> list[ProductSnapshot]:
        return [ProductSnapshot.of(p) for p in catalog.find_by_name(self.db, text)]

    def all_names(self) -> list[str]:
        from sqlalchemy import select

        from app.models import Product

        return list(self.db.scalars(select(Product.name)))


class ToolBox:
    """Wraps a CatalogPort with tracing and bounded retries for transient DB errors."""

    def __init__(self, port: CatalogPort, on_retry: Callable[[], None] | None = None) -> None:
        self.port = port
        self.calls: list[dict[str, Any]] = []
        self._on_retry = on_retry

    def _call(self, name: str, args: dict[str, Any], fn: Callable[[], T]) -> T:
        t0 = time.perf_counter()
        last: Exception | None = None
        for attempt in range(1, MAX_TOOL_ATTEMPTS + 1):
            try:
                result = fn()
                count = len(result) if isinstance(result, list) else None
                self.calls.append(
                    {
                        "tool": name,
                        "args": args,
                        "ok": True,
                        "attempts": attempt,
                        "result_count": count,
                        "ms": round((time.perf_counter() - t0) * 1000, 1),
                    }
                )
                return result
            except OperationalError as exc:  # transient: retry once after rollback
                last = exc
                if self._on_retry:
                    self._on_retry()
        self.calls.append(
            {
                "tool": name,
                "args": args,
                "ok": False,
                "attempts": MAX_TOOL_ATTEMPTS,
                "result_count": None,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        )
        assert last is not None
        raise last

    def search_catalog(self, category: str) -> list[ProductSnapshot]:
        return self._call("search_catalog", {"category": category}, lambda: self.port.products_in_category(category))

    def get_products(self, ids: list[int]) -> list[ProductSnapshot]:
        return self._call("get_products", {"ids": ids}, lambda: self.port.products_by_ids(ids))

    def find_products_by_name(self, text: str) -> list[ProductSnapshot]:
        return self._call("find_products_by_name", {"query": text[:80]}, lambda: self.port.find_by_name(text))

    def catalog_names(self) -> list[str]:
        return self._call("catalog_names", {}, self.port.all_names)


class InMemoryCatalog:
    """Catalogue port over a list of snapshots — used by unit tests and the evaluation suite."""

    def __init__(self, products: list[ProductSnapshot]) -> None:
        self.products = {p.id: p for p in products}

    def products_in_category(self, category: str) -> list[ProductSnapshot]:
        return [p for p in self.products.values() if p.category == category]

    def products_by_ids(self, ids: list[int]) -> list[ProductSnapshot]:
        return [self.products[i] for i in ids if i in self.products]

    def find_by_name(self, text: str) -> list[ProductSnapshot]:
        from app.services.catalog import _COMPARE_STOPWORDS, _tokens

        toks = set(_tokens(text)) - _COMPARE_STOPWORDS
        scored = []
        for p in self.products.values():
            overlap = toks & set(_tokens(p.name))
            if len(overlap) >= 2:
                scored.append((-len(overlap), -len(overlap) / len(_tokens(p.name)), p.id, p, overlap))
        scored.sort(key=lambda s: s[:3])
        covered: set[str] = set()
        out = []
        for *_, p, overlap in scored:
            if overlap - covered - {p.brand.lower()}:
                out.append(p)
                covered |= overlap
        return out[:4]

    def all_names(self) -> list[str]:
        return [p.name for p in self.products.values()]


def snapshots_from_seed() -> list[ProductSnapshot]:
    """Seed data as snapshots with deterministic ids (1..N in seed order)."""
    from app.seed.catalog_data import iter_products

    out = []
    for i, d in enumerate(iter_products(), start=1):
        out.append(
            ProductSnapshot(
                id=i,
                sku=d["sku"],
                name=d["name"],
                brand=d["brand"],
                category=d["category"],
                price_inr=d["price_inr"],
                mrp_inr=d["mrp_inr"],
                rating=d["rating"],
                review_count=d["review_count"],
                stock=d["stock"],
                specs=d["specs"],
                tags=tuple(d["tags"]),
            )
        )
    return out
