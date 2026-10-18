"""Cart, checkout quote and order tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, utcnow
from app.models.product import Product


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", cascade="all, delete-orphan", order_by="CartItem.id"
    )


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", name="uq_cart_items_cart_product"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)

    cart: Mapped[Cart] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(lazy="joined")


class CheckoutQuote(Base):
    """A priced snapshot of the cart. Checkout confirmation must reference a valid quote."""

    __tablename__ = "checkout_quotes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # [{"product_id", "quantity", "unit_price_inr"}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONType)
    subtotal_inr: Mapped[int] = mapped_column(Integer)
    shipping_inr: Mapped[int] = mapped_column(Integer)
    total_inr: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_orders_user_idempotency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    quote_id: Mapped[str] = mapped_column(String(36))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    payment_mode: Mapped[str] = mapped_column(String(20), default="demo")
    subtotal_inr: Mapped[int] = mapped_column(Integer)
    shipping_inr: Mapped[int] = mapped_column(Integer)
    total_inr: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(160))
    unit_price_inr: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    line_total_inr: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")
