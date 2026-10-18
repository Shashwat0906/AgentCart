from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, utcnow


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_inr > 0", name="price_positive"),
        CheckConstraint("mrp_inr >= price_inr", name="mrp_gte_price"),
        CheckConstraint("stock >= 0", name="stock_non_negative"),
        CheckConstraint("rating >= 0 AND rating <= 5", name="rating_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    brand: Mapped[str] = mapped_column(String(60), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str] = mapped_column(Text)
    price_inr: Mapped[int] = mapped_column(Integer, index=True)
    mrp_inr: Mapped[int] = mapped_column(Integer)
    rating: Mapped[float] = mapped_column(Float)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    specs: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list)
    # Lower-cased concatenation of name/brand/category/tags used for keyword search.
    search_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    @property
    def in_stock(self) -> bool:
        return self.stock > 0
