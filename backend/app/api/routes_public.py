"""Unauthenticated endpoints: health, auth and the product catalogue."""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_provider
from app.core.config import Settings, get_settings
from app.core.errors import ValidationFailed
from app.core.rate_limit import rate_limit
from app.core.security import create_access_token
from app.db.session import get_db
from app.models import Product, User
from app.providers import LLMProvider
from app.schemas import (
    ComparisonOut,
    LoginIn,
    ProductDetailOut,
    ProductOut,
    ProductPage,
    RegisterIn,
    TokenOut,
    UserOut,
)
from app.services import auth as auth_service
from app.services import catalog
from app.services.catalog import SortKey

router = APIRouter()
auth_limit = rate_limit("auth", lambda: get_settings().rate_limit_auth_per_minute)


# ------------------------------------------------------------------ health
@router.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", tags=["health"])
def ready(db: Session = Depends(get_db), provider: LLMProvider = Depends(get_provider)) -> JSONResponse:
    try:
        db.execute(text("SELECT 1"))
        products = db.scalar(select(func.count()).select_from(Product)) or 0
    except Exception:  # noqa: BLE001 - report not-ready instead of 500
        return JSONResponse({"status": "unavailable", "database": "down"}, status_code=503)
    return JSONResponse({"status": "ready", "database": "up", "products": products, "llm_provider": provider.name})


# ------------------------------------------------------------------ auth
def _token(user: User) -> TokenOut:
    token, expires = create_access_token(user.id)
    return TokenOut(access_token=token, expires_in=expires, user=UserOut.model_validate(user))


@router.post("/auth/register", response_model=TokenOut, status_code=201, tags=["auth"], dependencies=[Depends(auth_limit)])
def register(body: RegisterIn, db: Session = Depends(get_db)) -> TokenOut:
    return _token(auth_service.register_user(db, body.email, body.password, body.display_name))


@router.post("/auth/login", response_model=TokenOut, tags=["auth"], dependencies=[Depends(auth_limit)])
def login(body: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    return _token(auth_service.authenticate(db, body.email, body.password))


@router.get("/auth/me", response_model=UserOut, tags=["auth"])
def me(user: User = Depends(get_current_user)) -> User:
    return user


# ------------------------------------------------------------------ catalogue
def product_out(p: Product) -> dict[str, Any]:
    return {
        "id": p.id, "sku": p.sku, "name": p.name, "brand": p.brand, "category": p.category,
        "price_inr": p.price_inr, "mrp_inr": p.mrp_inr, "rating": p.rating, "review_count": p.review_count,
        "stock": p.stock, "in_stock": p.stock > 0, "tags": list(p.tags or []),
        "key_specs": catalog.key_specs(p.category, p.specs or {}),
    }


@router.get("/categories", tags=["catalog"])
def categories(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return catalog.category_summary(db)


@router.get("/brands", tags=["catalog"])
def brands(category: str | None = Query(default=None, max_length=40), db: Session = Depends(get_db)) -> list[str]:
    return catalog.brand_list(db, category)


@router.get("/products", response_model=ProductPage, tags=["catalog"])
def list_products(
    q: str | None = Query(default=None, max_length=120),
    category: str | None = Query(default=None, max_length=40),
    brand: list[str] | None = Query(default=None, max_length=20),
    min_price: int | None = Query(default=None, ge=0, le=10_000_000),
    max_price: int | None = Query(default=None, ge=0, le=10_000_000),
    min_rating: float | None = Query(default=None, ge=0, le=5),
    in_stock: bool | None = None,
    sort: SortKey = "relevance",
    page: int = Query(default=1, ge=1, le=1000),
    page_size: int = Query(default=24, ge=1, le=48),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = catalog.search_products(
        db, q=q, category=category, brands=brand, min_price=min_price, max_price=max_price,
        min_rating=min_rating, in_stock=in_stock, sort=sort, page=page, page_size=page_size,
    )
    return {"items": [product_out(p) for p in items], "total": total, "page": page, "page_size": page_size,
            "pages": max(1, math.ceil(total / page_size))}


@router.get("/products/compare", response_model=ComparisonOut, tags=["catalog"])
def compare(ids: str = Query(max_length=60, description="Comma-separated product ids (2–4)"), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        id_list = list(dict.fromkeys(int(x) for x in ids.split(",") if x.strip()))
    except ValueError as exc:
        raise ValidationFailed("ids must be comma-separated integers") from exc
    if not 2 <= len(id_list) <= 4:
        raise ValidationFailed("Compare between 2 and 4 products")
    products = catalog.products_by_ids(db, id_list)
    if len(products) != len(id_list):
        raise ValidationFailed("One or more products do not exist")
    table = catalog.comparison_table(products)
    return {**table, "products": [product_out(p) for p in products]}


@router.get("/products/{product_id}", response_model=ProductDetailOut, tags=["catalog"])
def product_detail(product_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    p = catalog.get_product(db, product_id)
    return {**product_out(p), "description": p.description, "specs": catalog.all_specs(p.category, p.specs or {}),
            "raw_specs": p.specs or {}}


@router.get("/config/public", tags=["health"])
def public_config(settings: Settings = Depends(get_settings), provider: LLMProvider = Depends(get_provider)) -> dict[str, Any]:
    return {
        "llm_provider": provider.name,
        "ranking_weights": settings.ranking_weights,
        "max_item_quantity": settings.max_item_quantity,
        "free_shipping_threshold_inr": settings.free_shipping_threshold_inr,
        "shipping_fee_inr": settings.shipping_fee_inr,
        "payment_mode": "demo",
    }
