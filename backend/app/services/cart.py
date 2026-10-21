"""Cart operations. All prices and totals are computed here from live product rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.models import Cart, CartItem, Product
from app.services.catalog import key_specs


def get_or_create_cart(db: Session, user_id: int) -> Cart:
    cart = db.scalar(select(Cart).where(Cart.user_id == user_id))
    if cart is None:
        try:
            with db.begin_nested():  # savepoint: losing the race below must not abort the caller's transaction
                cart = Cart(user_id=user_id)
                db.add(cart)
                db.flush()
        except IntegrityError:
            # A concurrent request created this user's cart first (carts.user_id is unique): use that one.
            cart = db.scalar(select(Cart).where(Cart.user_id == user_id))
            if cart is None:  # pragma: no cover - the conflicting row must exist once its insert committed
                raise
    return cart


def lock_product(db: Session, product_id: int) -> Product:
    """SELECT … FOR UPDATE on PostgreSQL so concurrent requests cannot oversell."""
    product = db.scalar(select(Product).where(Product.id == product_id).with_for_update())
    if product is None:
        raise NotFoundError("Product not found")
    return product


def shipping_for(subtotal: int, settings: Settings) -> int:
    if subtotal <= 0 or subtotal >= settings.free_shipping_threshold_inr:
        return 0
    return settings.shipping_fee_inr


def _check_quantity(quantity: int, settings: Settings) -> None:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise ValidationFailed("Quantity must be a whole number of at least 1")
    if quantity > settings.max_item_quantity:
        raise ValidationFailed(f"You can buy at most {settings.max_item_quantity} units of one product")


def add_item(
    db: Session, user_id: int, product_id: int, quantity: int, settings: Settings, *, commit: bool = True
) -> Cart:
    _check_quantity(quantity, settings)
    product = lock_product(db, product_id)
    cart = get_or_create_cart(db, user_id)
    item = next((i for i in cart.items if i.product_id == product_id), None)
    new_qty = quantity + (item.quantity if item else 0)
    if product.stock <= 0:
        raise ConflictError(f"{product.name} is out of stock", details={"product_id": product_id, "available": 0})
    if new_qty > product.stock:
        raise ConflictError(
            f"Only {product.stock} unit(s) of {product.name} available",
            details={"product_id": product_id, "available": product.stock, "in_cart": item.quantity if item else 0},
        )
    _check_quantity(new_qty, settings)
    if item is None:
        if len(cart.items) >= settings.max_cart_lines:
            raise ValidationFailed(f"Your cart can hold at most {settings.max_cart_lines} different products")
        cart.items.append(CartItem(product_id=product_id, quantity=quantity, product=product))
    else:
        item.quantity = new_qty
    if commit:
        db.commit()
    return cart


def set_quantity(db: Session, user_id: int, product_id: int, quantity: int, settings: Settings) -> Cart:
    _check_quantity(quantity, settings)
    cart = get_or_create_cart(db, user_id)
    item = next((i for i in cart.items if i.product_id == product_id), None)
    if item is None:
        raise NotFoundError("That product is not in your cart")
    product = lock_product(db, product_id)
    if quantity > product.stock:
        raise ConflictError(
            f"Only {product.stock} unit(s) of {product.name} available",
            details={"product_id": product_id, "available": product.stock},
        )
    item.quantity = quantity
    db.commit()
    return cart


def remove_item(db: Session, user_id: int, product_id: int) -> Cart:
    cart = get_or_create_cart(db, user_id)
    item = next((i for i in cart.items if i.product_id == product_id), None)
    if item is None:
        raise NotFoundError("That product is not in your cart")
    cart.items.remove(item)
    db.commit()
    return cart


def clear_cart(db: Session, user_id: int) -> Cart:
    cart = get_or_create_cart(db, user_id)
    cart.items.clear()
    db.commit()
    return cart


def cart_view(cart: Cart, settings: Settings) -> dict[str, Any]:
    items = []
    subtotal = 0
    issues: list[str] = []
    for it in cart.items:
        p = it.product
        line = p.price_inr * it.quantity
        subtotal += line
        problem = None
        if p.stock <= 0:
            problem = "Out of stock"
        elif it.quantity > p.stock:
            problem = f"Only {p.stock} available"
        if problem:
            issues.append(f"{p.name}: {problem}")
        items.append(
            {
                "product_id": p.id,
                "sku": p.sku,
                "name": p.name,
                "brand": p.brand,
                "category": p.category,
                "unit_price_inr": p.price_inr,
                "mrp_inr": p.mrp_inr,
                "quantity": it.quantity,
                "line_total_inr": line,
                "stock": p.stock,
                "problem": problem,
                "key_specs": key_specs(p.category, p.specs),
            }
        )
    shipping = shipping_for(subtotal, settings)
    return {
        "items": items,
        "item_count": sum(i["quantity"] for i in items),
        "subtotal_inr": subtotal,
        "shipping_inr": shipping,
        "total_inr": subtotal + shipping,
        "free_shipping_threshold_inr": settings.free_shipping_threshold_inr,
        "max_item_quantity": settings.max_item_quantity,
        "issues": issues,
    }
