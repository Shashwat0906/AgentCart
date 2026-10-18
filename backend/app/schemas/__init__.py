"""Pydantic request/response models. Strict types on anything that changes state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StrictInt, field_validator


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


# ------------------------------------------------------------------ auth
class RegisterIn(Model):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        if v.strip() != v or len(set(v)) < 4:
            raise ValueError("password is too weak")
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("password must contain letters and numbers")
        return v

    @field_validator("display_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or any(ch in v for ch in "<>"):
            raise ValueError("invalid display name")
        return v


class LoginIn(Model):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(Model):
    id: int
    email: str
    display_name: str
    created_at: datetime


class TokenOut(Model):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - token type, not a secret
    expires_in: int
    user: UserOut


# ------------------------------------------------------------------ products
class SpecOut(Model):
    key: str
    label: str
    value: str


class ProductOut(Model):
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
    in_stock: bool
    tags: list[str]
    key_specs: list[SpecOut]


class ProductDetailOut(ProductOut):
    description: str
    specs: list[SpecOut]
    raw_specs: dict[str, Any]


class ProductPage(Model):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int
    pages: int


class ComparisonOut(Model):
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    products: list[ProductOut]


# ------------------------------------------------------------------ cart
class CartItemIn(Model):
    product_id: StrictInt = Field(ge=1)
    quantity: StrictInt = Field(default=1, ge=1, le=100)


class QuantityIn(Model):
    quantity: StrictInt = Field(ge=1, le=100)


class CartOut(Model):
    items: list[dict[str, Any]]
    item_count: int
    subtotal_inr: int
    shipping_inr: int
    total_inr: int
    free_shipping_threshold_inr: int
    max_item_quantity: int
    issues: list[str]


# ------------------------------------------------------------------ chat & actions
class RankingWeights(Model):
    budget: float = Field(default=0.25, ge=0, le=1)
    spec: float = Field(default=0.30, ge=0, le=1)
    rating: float = Field(default=0.20, ge=0, le=1)
    preference: float = Field(default=0.25, ge=0, le=1)


class ChatIn(Model):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    weights: RankingWeights | None = None


class ActionOut(Model):
    id: str
    kind: str
    status: str
    summary: str
    payload: dict[str, Any]
    result: dict[str, Any]
    conversation_id: str | None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None


class MessageOut(Model):
    id: int
    role: str
    content: str
    payload: dict[str, Any]
    created_at: datetime


class ChatOut(Model):
    conversation_id: str
    title: str
    user_message: MessageOut
    assistant_message: MessageOut
    actions: list[ActionOut]


class ConversationSummary(Model):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class ConversationDetail(Model):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]


class ApprovalOut(Model):
    action: ActionOut
    cart: CartOut | None = None


# ------------------------------------------------------------------ preferences
PreferenceKind = Literal["preferred_brand", "avoided_brand", "budget_max", "use_case", "note"]


class PreferenceIn(Model):
    kind: PreferenceKind
    value: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=40)


class PreferenceUpdate(Model):
    value: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=40)


class PreferenceOut(Model):
    id: int
    kind: str
    value: str
    category: str | None
    source: str
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ checkout
class QuoteOut(Model):
    quote_id: str
    expires_at: datetime
    items: list[dict[str, Any]]
    subtotal_inr: int
    shipping_inr: int
    total_inr: int
    payment_mode: Literal["demo"] = "demo"


class ConfirmIn(Model):
    quote_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    confirm: Literal[True]
    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class OrderItemOut(Model):
    product_id: int | None
    sku: str
    name: str
    unit_price_inr: int
    quantity: int
    line_total_inr: int


class OrderOut(Model):
    id: int
    status: str
    payment_mode: str
    subtotal_inr: int
    shipping_inr: int
    total_inr: int
    created_at: datetime
    items: list[OrderItemOut]
