"""Authenticated, user-scoped endpoints. Every query is filtered by the caller's user id."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import chat_limit, get_current_user, get_provider, write_limit
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models import User
from app.providers import LLMProvider
from app.schemas import (
    ActionOut,
    ApprovalOut,
    CartItemIn,
    CartOut,
    ChatIn,
    ChatOut,
    ConfirmIn,
    ConversationDetail,
    ConversationSummary,
    OrderOut,
    PreferenceIn,
    PreferenceOut,
    PreferenceUpdate,
    QuantityIn,
    QuoteOut,
)
from app.services import actions as action_service
from app.services import cart as cart_service
from app.services import chat as chat_service
from app.services import checkout as checkout_service
from app.services import preferences as pref_service

router = APIRouter()


# ------------------------------------------------------------------ cart
@router.get("/cart", response_model=CartOut, tags=["cart"])
def get_cart(
    user: User = Depends(get_current_user), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    cart = cart_service.get_or_create_cart(db, user.id)
    db.commit()
    return cart_service.cart_view(cart, settings)


@router.post("/cart/items", response_model=CartOut, tags=["cart"], dependencies=[Depends(write_limit)])
def add_to_cart(
    body: CartItemIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return cart_service.cart_view(
        cart_service.add_item(db, user.id, body.product_id, body.quantity, settings), settings
    )


@router.patch("/cart/items/{product_id}", response_model=CartOut, tags=["cart"], dependencies=[Depends(write_limit)])
def update_quantity(
    product_id: int,
    body: QuantityIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return cart_service.cart_view(cart_service.set_quantity(db, user.id, product_id, body.quantity, settings), settings)


@router.delete("/cart/items/{product_id}", response_model=CartOut, tags=["cart"], dependencies=[Depends(write_limit)])
def remove_from_cart(
    product_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return cart_service.cart_view(cart_service.remove_item(db, user.id, product_id), settings)


@router.delete("/cart", response_model=CartOut, tags=["cart"], dependencies=[Depends(write_limit)])
def clear_cart(
    user: User = Depends(get_current_user), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    return cart_service.cart_view(cart_service.clear_cart(db, user.id), settings)


# ------------------------------------------------------------------ chat
@router.post("/chat", response_model=ChatOut, tags=["agent"], dependencies=[Depends(chat_limit)])
def chat(
    body: ChatIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    provider: LLMProvider = Depends(get_provider),
) -> dict[str, Any]:
    weights = body.weights.model_dump() if body.weights else settings.ranking_weights
    conv, user_msg, assistant, actions, _ = chat_service.handle_message(
        db, user.id, body.message, body.conversation_id, weights, settings, provider
    )
    return {
        "conversation_id": conv.id,
        "title": conv.title,
        "user_message": chat_service.message_view(user_msg),
        "assistant_message": chat_service.message_view(assistant, actions),
        "actions": [action_service.action_view(a) for a in actions],
    }


@router.get("/conversations", response_model=list[ConversationSummary], tags=["agent"])
def conversations(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return chat_service.list_conversations(db, user.id)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail, tags=["agent"])
def conversation(
    conversation_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return chat_service.conversation_detail(db, chat_service.get_conversation(db, user.id, conversation_id))


@router.delete("/conversations/{conversation_id}", status_code=204, tags=["agent"], dependencies=[Depends(write_limit)])
def delete_conversation(
    conversation_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Response:
    chat_service.delete_conversation(db, user.id, conversation_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ approvals
@router.get("/actions", response_model=list[ActionOut], tags=["agent"])
def list_actions(
    status: str | None = Query(default=None, pattern="^(pending|executed|rejected|failed|expired|superseded)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return [action_service.action_view(a) for a in action_service.list_actions(db, user.id, status)]


@router.post(
    "/actions/{action_id}/approve", response_model=ApprovalOut, tags=["agent"], dependencies=[Depends(write_limit)]
)
def approve_action(
    action_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    action = action_service.approve(db, user.id, action_id, settings)
    cart = None
    if action.kind == "add_to_cart":
        cart = cart_service.cart_view(cart_service.get_or_create_cart(db, user.id), settings)
    return {"action": action_service.action_view(action), "cart": cart}


@router.post(
    "/actions/{action_id}/reject", response_model=ApprovalOut, tags=["agent"], dependencies=[Depends(write_limit)]
)
def reject_action(
    action_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> dict[str, Any]:
    return {"action": action_service.action_view(action_service.reject(db, user.id, action_id)), "cart": None}


# ------------------------------------------------------------------ preferences
@router.get("/preferences", response_model=list[PreferenceOut], tags=["preferences"])
def get_preferences(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[Any]:
    return pref_service.list_preferences(db, user.id)


@router.post(
    "/preferences",
    response_model=PreferenceOut,
    status_code=201,
    tags=["preferences"],
    dependencies=[Depends(write_limit)],
)
def create_preference(body: PreferenceIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Any:
    return pref_service.create_preference(db, user.id, body.kind, body.value, body.category)


@router.patch(
    "/preferences/{pref_id}", response_model=PreferenceOut, tags=["preferences"], dependencies=[Depends(write_limit)]
)
def update_preference(
    pref_id: int, body: PreferenceUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Any:
    return pref_service.update_preference(db, user.id, pref_id, body.value, body.category)


@router.delete("/preferences/{pref_id}", status_code=204, tags=["preferences"], dependencies=[Depends(write_limit)])
def delete_preference(pref_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    pref_service.delete_preference(db, user.id, pref_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ checkout & orders
@router.post("/checkout/quote", response_model=QuoteOut, tags=["checkout"], dependencies=[Depends(write_limit)])
def quote(
    user: User = Depends(get_current_user), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    q, view = checkout_service.create_quote(db, user.id, settings)
    return {
        "quote_id": q.id,
        "expires_at": q.expires_at,
        "items": view["items"],
        "subtotal_inr": q.subtotal_inr,
        "shipping_inr": q.shipping_inr,
        "total_inr": q.total_inr,
    }


@router.post("/checkout/confirm", response_model=OrderOut, tags=["checkout"], dependencies=[Depends(write_limit)])
def confirm(
    body: ConfirmIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    order, created = checkout_service.confirm(db, user.id, body.quote_id, body.idempotency_key, settings)
    response.status_code = 201 if created else 200
    return checkout_service.order_view(order)


@router.get("/orders", response_model=list[OrderOut], tags=["checkout"])
def orders(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [checkout_service.order_view(o) for o in checkout_service.list_orders(db, user.id)]


@router.get("/orders/{order_id}", response_model=OrderOut, tags=["checkout"])
def order(order_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return checkout_service.order_view(checkout_service.get_order(db, user.id, order_id))


def new_idempotency_key() -> str:  # helper for clients/tests
    return uuid.uuid4().hex
