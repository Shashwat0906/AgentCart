"""Demo checkout: quote → explicit confirmation → order. No real payment is ever taken."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.models import CheckoutQuote, Order, OrderItem, Product
from app.services.cart import cart_view, get_or_create_cart, shipping_for
from app.services.specs import format_inr


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def create_quote(db: Session, user_id: int, settings: Settings) -> tuple[CheckoutQuote, dict[str, Any]]:
    cart = get_or_create_cart(db, user_id)
    view = cart_view(cart, settings)
    if not view["items"]:
        raise ValidationFailed("Your cart is empty")
    if view["issues"]:
        raise ConflictError("Some items can't be ordered right now", details={"issues": view["issues"]})
    quote = CheckoutQuote(
        id=str(uuid.uuid4()), user_id=user_id,
        items=[{"product_id": i["product_id"], "quantity": i["quantity"], "unit_price_inr": i["unit_price_inr"]} for i in view["items"]],
        subtotal_inr=view["subtotal_inr"], shipping_inr=view["shipping_inr"], total_inr=view["total_inr"],
        expires_at=_now() + timedelta(seconds=settings.checkout_quote_ttl_seconds),
    )
    db.add(quote)
    db.commit()
    return quote, view


def confirm(db: Session, user_id: int, quote_id: str, idempotency_key: str, settings: Settings) -> tuple[Order, bool]:
    """Returns (order, created). Re-validates everything inside one transaction."""
    existing = db.scalar(select(Order).where(Order.user_id == user_id, Order.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.quote_id != quote_id:
            raise ConflictError("This idempotency key was already used for a different checkout")
        return existing, False

    quote = db.get(CheckoutQuote, quote_id)
    if quote is None or quote.user_id != user_id:
        raise NotFoundError("Checkout quote not found")
    if quote.used_at is not None:
        raise ConflictError("This checkout quote was already used", details={"reason": "quote_used"})
    if _aware(quote.expires_at) <= _now():
        raise ConflictError("Your checkout quote expired. Please review your cart again.", details={"reason": "quote_expired"})

    cart = get_or_create_cart(db, user_id)
    cart_state = sorted((i.product_id, i.quantity) for i in cart.items)
    quoted_state = sorted((int(i["product_id"]), int(i["quantity"])) for i in quote.items)
    if cart_state != quoted_state:
        raise ConflictError("Your cart changed after the quote. Please review it again.", details={"reason": "cart_changed"})

    # Lock rows in a stable order to avoid deadlocks, then re-check price and stock.
    ids = sorted(pid for pid, _ in quoted_state)
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids)).order_by(Product.id).with_for_update())}
    price_changes, stock_problems = [], []
    for item in quote.items:
        p = products.get(int(item["product_id"]))
        if p is None:
            stock_problems.append({"product_id": item["product_id"], "name": "Unavailable product", "available": 0})
            continue
        if p.price_inr != int(item["unit_price_inr"]):
            price_changes.append({"product_id": p.id, "name": p.name, "quoted_price_inr": item["unit_price_inr"], "current_price_inr": p.price_inr})
        if p.stock < int(item["quantity"]):
            stock_problems.append({"product_id": p.id, "name": p.name, "requested": item["quantity"], "available": p.stock})
    if price_changes:
        db.rollback()
        first = price_changes[0]
        raise ConflictError(
            f"The price of {first['name']} changed from {format_inr(first['quoted_price_inr'])} to "
            f"{format_inr(first['current_price_inr'])}. Please review the new total.",
            details={"reason": "price_changed", "changes": price_changes},
        )
    if stock_problems:
        db.rollback()
        raise ConflictError("Some items no longer have enough stock", details={"reason": "insufficient_stock", "items": stock_problems})

    subtotal = sum(products[int(i["product_id"])].price_inr * int(i["quantity"]) for i in quote.items)
    shipping = shipping_for(subtotal, settings)
    order = Order(user_id=user_id, quote_id=quote.id, idempotency_key=idempotency_key, status="confirmed",
                  payment_mode="demo", subtotal_inr=subtotal, shipping_inr=shipping, total_inr=subtotal + shipping)
    for item in quote.items:
        p = products[int(item["product_id"])]
        qty = int(item["quantity"])
        p.stock -= qty
        order.items.append(OrderItem(product_id=p.id, sku=p.sku, name=p.name, unit_price_inr=p.price_inr,
                                     quantity=qty, line_total_inr=p.price_inr * qty))
    db.add(order)
    quote.used_at = _now()
    cart.items.clear()
    try:
        db.commit()
    except IntegrityError as exc:  # concurrent confirm with the same idempotency key
        db.rollback()
        again = db.scalar(select(Order).where(Order.user_id == user_id, Order.idempotency_key == idempotency_key))
        if again is not None:
            return again, False
        raise ConflictError("Checkout could not be completed. Please try again.") from exc
    return order, True


def list_orders(db: Session, user_id: int) -> list[Order]:
    return list(db.scalars(select(Order).where(Order.user_id == user_id).order_by(Order.created_at.desc(), Order.id.desc())))


def get_order(db: Session, user_id: int, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None or order.user_id != user_id:
        raise NotFoundError("Order not found")
    return order


def order_view(o: Order) -> dict[str, Any]:
    return {
        "id": o.id, "status": o.status, "payment_mode": o.payment_mode, "subtotal_inr": o.subtotal_inr,
        "shipping_inr": o.shipping_inr, "total_inr": o.total_inr, "created_at": o.created_at,
        "items": [{"product_id": i.product_id, "sku": i.sku, "name": i.name, "unit_price_inr": i.unit_price_inr,
                   "quantity": i.quantity, "line_total_inr": i.line_total_inr} for i in o.items],
    }
