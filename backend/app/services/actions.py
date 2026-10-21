"""Approval gate for agent-proposed actions.

An action is created `pending`. Only its owner can approve or reject it. On approval we
re-read live data: if the price changed since the proposal, the action is superseded by a
fresh proposal (the user must approve again); if stock is insufficient it fails. Nothing is
ever executed implicitly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.models import AgentAction
from app.services import cart as cart_service
from app.services import preferences as pref_service
from app.services.specs import format_inr


class ActionGone(AppError):
    status_code = 410
    code = "action_not_pending"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def persist_actions(
    db: Session, user_id: int, conversation_id: str | None, proposals: list[dict[str, Any]], settings: Settings
) -> list[AgentAction]:
    if proposals and conversation_id:
        # Older pending proposals in this conversation are superseded so stale ones can't be approved later.
        for old in db.scalars(
            select(AgentAction).where(AgentAction.conversation_id == conversation_id, AgentAction.status == "pending")
        ):
            old.status = "superseded"
            old.decided_at = _now()
    out = []
    for p in proposals:
        action = AgentAction(
            id=str(uuid.uuid4()),
            user_id=user_id,
            conversation_id=conversation_id,
            kind=p["kind"],
            payload=p["payload"],
            summary=p["summary"][:300],
            status="pending",
            expires_at=_now() + timedelta(minutes=settings.agent_action_ttl_minutes),
        )
        db.add(action)
        out.append(action)
    db.flush()
    return out


def get_owned(db: Session, user_id: int, action_id: str) -> AgentAction:
    action = db.get(AgentAction, action_id)
    if action is None or action.user_id != user_id:  # don't reveal other users' actions
        raise NotFoundError("Action not found")
    return action


def _lock_owned(db: Session, user_id: int, action_id: str) -> AgentAction:
    """The caller's action, row-locked until this transaction ends. Deciding an action reads its status under
    this lock, so concurrent approve/reject requests (double click, retry, two tabs) are serialised: the first
    one decides, the others see the new status and change nothing."""
    action = db.get(AgentAction, action_id, with_for_update=True, populate_existing=True)
    if action is None or action.user_id != user_id:
        db.rollback()  # release the lock; don't reveal other users' actions
        raise NotFoundError("Action not found")
    return action


def list_actions(db: Session, user_id: int, status: str | None = None) -> list[AgentAction]:
    stmt = select(AgentAction).where(AgentAction.user_id == user_id).order_by(AgentAction.created_at.desc())
    if status:
        stmt = stmt.where(AgentAction.status == status)
    return list(db.scalars(stmt.limit(100)))


def _ensure_pending(db: Session, action: AgentAction) -> None:
    if action.status == "pending" and _aware(action.expires_at) <= _now():
        action.status = "expired"
        action.decided_at = _now()
        db.commit()
    if action.status != "pending":
        raise ActionGone(f"This action is already {action.status}", details={"status": action.status})


def reject(db: Session, user_id: int, action_id: str) -> AgentAction:
    action = _lock_owned(db, user_id, action_id)
    _ensure_pending(db, action)
    action.status = "rejected"
    action.decided_at = _now()
    db.commit()
    return action


def approve(db: Session, user_id: int, action_id: str, settings: Settings) -> AgentAction:
    action = _lock_owned(db, user_id, action_id)
    _ensure_pending(db, action)
    if action.kind == "add_to_cart":
        _execute_add_to_cart(db, user_id, action, settings)
    elif action.kind == "save_preference":
        _execute_save_preference(db, user_id, action)
    else:  # pragma: no cover - only known kinds are ever created
        raise ConflictError("Unsupported action")
    return action


def _fail(db: Session, action: AgentAction, reason: str, extra: dict[str, Any] | None = None) -> None:
    db.rollback()  # discard partial cart changes; `action` stays attached to the session
    action.status = "failed"
    action.decided_at = _now()
    action.result = {"reason": reason, **(extra or {})}
    db.commit()


def _execute_add_to_cart(db: Session, user_id: int, action: AgentAction, settings: Settings) -> None:
    payload = action.payload
    product_id, qty, expected = int(payload["product_id"]), int(payload["quantity"]), int(payload["expected_price_inr"])
    try:
        product = cart_service.lock_product(db, product_id)
    except NotFoundError:
        _fail(db, action, "product_not_found")
        raise ConflictError("That product no longer exists") from None

    if product.price_inr != expected:
        action.status = "superseded"
        action.decided_at = _now()
        action.result = {"reason": "price_changed", "old_price_inr": expected, "new_price_inr": product.price_inr}
        fresh = persist_actions(
            db,
            user_id,
            None,
            [
                {
                    "kind": "add_to_cart",
                    "payload": {**payload, "expected_price_inr": product.price_inr},
                    "summary": f"Add {qty} × {product.name} ({format_inr(product.price_inr)} each) to your cart",
                }
            ],
            settings,
        )[0]
        fresh.conversation_id = action.conversation_id
        fresh.task_id = action.task_id  # still the task's proposal; the step link stays with the original
        db.commit()
        raise ConflictError(
            f"The price of {product.name} changed from {format_inr(expected)} to {format_inr(product.price_inr)}. "
            "Please review and approve the updated proposal.",
            details={
                "reason": "price_changed",
                "old_price_inr": expected,
                "new_price_inr": product.price_inr,
                "new_action_id": fresh.id,
            },
        )
    try:
        cart_service.add_item(db, user_id, product_id, qty, settings, commit=False)
    except AppError as exc:
        _fail(db, action, "cart_rejected", {"message": exc.message, **exc.details})
        raise ConflictError(exc.message, details={"reason": "cart_rejected", **exc.details}) from None
    action.status = "executed"
    action.decided_at = _now()
    action.result = {"product_id": product_id, "quantity": qty, "unit_price_inr": product.price_inr}
    db.commit()


def _execute_save_preference(db: Session, user_id: int, action: AgentAction) -> None:
    p = action.payload
    try:
        pref = pref_service.create_preference(db, user_id, p["kind"], p["value"], p.get("category"), source="agent")
    except AppError as exc:
        _fail(db, action, "preference_rejected", {"message": exc.message})
        raise
    action.status = "executed"
    action.decided_at = _now()
    action.result = {"preference_id": pref.id}
    db.commit()


def action_view(a: AgentAction) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "status": a.status,
        "summary": a.summary,
        "payload": a.payload,
        "result": a.result or {},
        "conversation_id": a.conversation_id,
        "task_id": a.task_id,
        "created_at": a.created_at,
        "expires_at": a.expires_at,
        "decided_at": a.decided_at,
    }
