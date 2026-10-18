"""Idempotent catalogue seeding."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.seed.catalog_data import iter_products


def seed_catalog(session: Session, *, reset_prices_and_stock: bool = False) -> dict[str, int]:
    """Insert missing products by SKU. Existing rows are left untouched unless
    ``reset_prices_and_stock`` is set, in which case price/stock/specs are restored
    to the seed values (useful after demo checkouts have depleted stock)."""
    existing = {p.sku: p for p in session.scalars(select(Product))}
    inserted = updated = 0
    for data in iter_products():
        product = existing.get(data["sku"])
        if product is None:
            session.add(Product(**data))
            inserted += 1
        elif reset_prices_and_stock:
            for key, value in data.items():
                setattr(product, key, value)
            updated += 1
    session.commit()
    return {"inserted": inserted, "updated": updated, "total": len(existing) + inserted}
