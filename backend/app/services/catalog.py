"""Catalogue queries, product snapshots and comparison tables."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import ColumnElement, Select, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailed
from app.models import Product
from app.services.specs import CATEGORY_LABELS, CATEGORY_SPECS, COMMON_FIELDS, SpecField, format_spec_value

SortKey = Literal["relevance", "price_asc", "price_desc", "rating", "newest", "name"]

KEY_SPECS: dict[str, list[str]] = {
    "laptops": ["cpu", "ram_gb", "storage_gb", "gpu", "battery_hours", "weight_kg"],
    "smartphones": ["chipset", "ram_gb", "storage_gb", "battery_mah", "main_camera_mp"],
    "headphones": ["form_factor", "anc", "battery_hours", "wireless"],
    "monitors": ["size_inches", "resolution", "refresh_hz", "panel"],
    "tablets": ["ram_gb", "storage_gb", "display_inches", "stylus_support"],
    "smartwatches": ["battery_days", "gps", "display", "compatibility"],
    "keyboards": ["layout", "mechanical", "wireless", "backlit"],
    "mice": ["dpi", "wireless", "weight_g", "ergonomic"],
    "speakers": ["battery_hours", "waterproof", "output_w"],
}


@dataclass(frozen=True)
class ProductSnapshot:
    """Immutable copy of a product row, decoupled from the DB session.

    The agent reasons over snapshots; anything that mutates state re-reads the live row.
    """

    id: int
    sku: str
    name: str
    brand: str
    category: str
    price_inr: int
    mrp_inr: int
    rating: float
    review_count: int
    stock: int
    specs: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()

    @classmethod
    def of(cls, p: Product) -> ProductSnapshot:
        return cls(
            id=p.id,
            sku=p.sku,
            name=p.name,
            brand=p.brand,
            category=p.category,
            price_inr=p.price_inr,
            mrp_inr=p.mrp_inr,
            rating=p.rating,
            review_count=p.review_count,
            stock=p.stock,
            specs=dict(p.specs or {}),
            tags=tuple(p.tags or ()),
        )


_SEARCH_STOPWORDS = {"or", "and", "the", "for", "with", "a", "an", "of", "to", "in", "on", "my", "me", "is", "it"}


def _tokens(q: str) -> list[str]:
    """Lower-cased alphanumeric tokens. LIKE wildcards and quotes never survive tokenisation,
    and values are bound parameters with autoescape, so search input cannot alter the query."""
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.+-]*", q.lower()) if len(t) >= 2 and t not in _SEARCH_STOPWORDS][:8]


def search_products(
    db: Session,
    *,
    q: str | None = None,
    category: str | None = None,
    brands: list[str] | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    min_rating: float | None = None,
    in_stock: bool | None = None,
    sort: SortKey = "relevance",
    page: int = 1,
    page_size: int = 24,
) -> tuple[list[Product], int]:
    if category and category not in CATEGORY_SPECS:
        raise ValidationFailed(f"Unknown category '{category}'")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValidationFailed("min_price cannot be greater than max_price")

    stmt: Select[tuple[Product]] = select(Product)
    tokens = _tokens(q or "")
    for tok in tokens:
        stmt = stmt.where(
            or_(
                Product.search_text.contains(tok, autoescape=True),
                func.lower(Product.description).contains(tok, autoescape=True),
            )
        )
    if category:
        stmt = stmt.where(Product.category == category)
    if brands:
        stmt = stmt.where(func.lower(Product.brand).in_([b.lower() for b in brands]))
    if min_price is not None:
        stmt = stmt.where(Product.price_inr >= min_price)
    if max_price is not None:
        stmt = stmt.where(Product.price_inr <= max_price)
    if min_rating is not None:
        stmt = stmt.where(Product.rating >= min_rating)
    if in_stock:
        stmt = stmt.where(Product.stock > 0)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    if sort == "price_asc":
        stmt = stmt.order_by(Product.price_inr.asc(), Product.id)
    elif sort == "price_desc":
        stmt = stmt.order_by(Product.price_inr.desc(), Product.id)
    elif sort == "rating":
        stmt = stmt.order_by(Product.rating.desc(), Product.review_count.desc(), Product.id)
    elif sort == "newest":
        stmt = stmt.order_by(Product.created_at.desc(), Product.id.desc())
    elif sort == "name":
        stmt = stmt.order_by(Product.name.asc(), Product.id)
    else:
        if tokens:
            terms = [case((func.lower(Product.name).contains(t, autoescape=True), 1), else_=0) for t in tokens]
            name_hits: ColumnElement[Any] = terms[0]
            for term in terms[1:]:
                name_hits = name_hits + term
            stmt = stmt.order_by(name_hits.desc(), (Product.stock > 0).desc(), Product.rating.desc(), Product.id)
        else:
            stmt = stmt.order_by(
                (Product.stock > 0).desc(), Product.rating.desc(), Product.review_count.desc(), Product.id
            )

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    return list(db.scalars(stmt)), int(total)


def get_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError("Product not found")
    return product


def products_by_ids(db: Session, ids: list[int]) -> list[Product]:
    if not ids:
        return []
    rows = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)))}
    return [rows[i] for i in ids if i in rows]


def products_in_category(db: Session, category: str) -> list[Product]:
    return list(db.scalars(select(Product).where(Product.category == category).order_by(Product.id)))


_COMPARE_STOPWORDS = {
    "compare",
    "vs",
    "versus",
    "and",
    "the",
    "with",
    "between",
    "which",
    "better",
    "difference",
    "or",
    "is",
    "of",
    "me",
    "show",
    "please",
}


def find_by_name(db: Session, text: str, limit: int = 4) -> list[Product]:
    """Match products mentioned by (partial) name, e.g. 'compare iPhone 15 and Galaxy S24'.

    Requires ≥2 overlapping name tokens; then greedily keeps products that cover query
    tokens not already covered, so near-duplicates (iPhone 15 vs 15 Pro) are not both picked.
    """
    toks = set(_tokens(text)) - _COMPARE_STOPWORDS
    if not toks:
        return []
    scored: list[tuple[int, float, Product, set[str]]] = []
    for p in db.scalars(select(Product)):
        name_toks = set(_tokens(p.name))
        overlap = toks & name_toks
        if len(overlap) >= 2:
            scored.append((len(overlap), len(overlap) / len(name_toks), p, overlap))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2].id))
    covered: set[str] = set()
    out: list[Product] = []
    for _, _, p, overlap in scored:
        if overlap - covered - {p.brand.lower()}:
            out.append(p)
            covered |= overlap
        if len(out) >= limit:
            break
    return out


def category_summary(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Product.category, func.count(), func.min(Product.price_inr), func.max(Product.price_inr)).group_by(
            Product.category
        )
    ).all()
    counts = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in rows}
    out = []
    for key, label in CATEGORY_LABELS.items():
        n, lo, hi = counts.get(key, (0, 0, 0))
        out.append(
            {
                "key": key,
                "label": label,
                "product_count": n,
                "min_price_inr": lo,
                "max_price_inr": hi,
                "specs": [
                    {"key": f.key, "label": f.label, "type": f.type, "unit": f.unit} for f in CATEGORY_SPECS[key]
                ],
            }
        )
    return out


def brand_list(db: Session, category: str | None = None) -> list[str]:
    stmt = select(Product.brand).distinct().order_by(Product.brand)
    if category:
        stmt = stmt.where(Product.category == category)
    return list(db.scalars(stmt))


def key_specs(category: str, specs: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for key in KEY_SPECS.get(category, []):
        f = next((s for s in CATEGORY_SPECS[category] if s.key == key), None)
        if f is not None and key in specs:
            out.append({"key": key, "label": f.label, "value": format_spec_value(f, specs[key])})
    return out


def all_specs(category: str, specs: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"key": f.key, "label": f.label, "value": format_spec_value(f, specs.get(f.key))}
        for f in CATEGORY_SPECS.get(category, [])
        if f.key in specs
    ]


def _best_index(f: SpecField, values: list[Any]) -> list[int]:
    if f.type == "bool":
        if any(values) and not all(values):
            return [i for i, v in enumerate(values) if v]
        return []
    if f.type != "number" or f.higher_is_better is None:
        return []
    nums = [(i, float(v)) for i, v in enumerate(values) if isinstance(v, int | float) and not isinstance(v, bool)]
    if len(nums) < 2 or len({v for _, v in nums}) < 2:
        return []
    target = max(v for _, v in nums) if f.higher_is_better else min(v for _, v in nums)
    return [i for i, v in nums if v == target]


def comparison_table(products: list[Any]) -> dict[str, Any]:
    """Side-by-side table built only from catalogue fields. `best` marks winning cells."""
    if not products:
        return {"columns": [], "rows": []}
    categories = {p.category for p in products}
    rows: list[dict[str, Any]] = []
    for key in ("price_inr", "rating"):
        f = COMMON_FIELDS[key]
        values = [getattr(p, key) for p in products]
        rows.append(
            {
                "key": key,
                "label": f.label,
                "values": [format_spec_value(f, v) if key == "price_inr" else f"{v:.1f}★" for v in values],
                "best": _best_index(f, values),
            }
        )
    rows.append(
        {
            "key": "stock",
            "label": "Availability",
            "values": ["In stock" if p.stock > 0 else "Out of stock" for p in products],
            "best": [],
        }
    )
    if len(categories) == 1:
        cat = next(iter(categories))
        for f in CATEGORY_SPECS.get(cat, []):
            values = [p.specs.get(f.key) for p in products]
            if all(v is None for v in values):
                continue
            rows.append(
                {
                    "key": f.key,
                    "label": f.label,
                    "values": [format_spec_value(f, v) for v in values],
                    "best": _best_index(f, values),
                }
            )
    return {
        "columns": [{"product_id": p.id, "name": p.name, "brand": p.brand, "category": p.category} for p in products],
        "rows": rows,
    }
