"""Conversation persistence around the agent workflow."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.graph import AgentDeps, AgentResult, run_agent
from app.agents.guardrails import sanitize_user_text
from app.agents.memory import build_context
from app.agents.tools import DbCatalog, ToolBox
from app.core.config import Settings
from app.core.errors import NotFoundError, ValidationFailed
from app.db.base import utcnow
from app.models import AgentAction, Conversation, Message
from app.providers.base import LLMProvider
from app.services import actions as action_service
from app.services import preferences as pref_service

MAX_MESSAGES_PER_CONVERSATION = 200


def get_conversation(db: Session, user_id: int, conversation_id: str) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user_id:
        raise NotFoundError("Conversation not found")
    return conv


def list_conversations(db: Session, user_id: int) -> list[dict[str, Any]]:
    counts = select(Message.conversation_id, func.count().label("n")).group_by(Message.conversation_id).subquery()
    rows = db.execute(
        select(Conversation, counts.c.n)
        .outerjoin(counts, counts.c.conversation_id == Conversation.id)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": c.id,
            "title": c.title,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
            "message_count": int(n or 0),
        }
        for c, n in rows
    ]


def delete_conversation(db: Session, user_id: int, conversation_id: str) -> None:
    db.delete(get_conversation(db, user_id, conversation_id))
    db.commit()


def message_view(m: Message, actions: list[AgentAction] | None = None) -> dict[str, Any]:
    payload = dict(m.payload or {})
    if actions is not None:
        ids = set(payload.get("action_ids") or [])
        payload["actions"] = [action_service.action_view(a) for a in actions if a.id in ids]
    return {"id": m.id, "role": m.role, "content": m.content, "payload": payload, "created_at": m.created_at}


def conversation_detail(db: Session, conv: Conversation) -> dict[str, Any]:
    actions = list(db.scalars(select(AgentAction).where(AgentAction.conversation_id == conv.id)))
    return {
        "id": conv.id,
        "title": conv.title,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "messages": [message_view(m, actions) for m in conv.messages],
    }


def _title(text: str) -> str:
    t = " ".join(text.split())
    return (t[:57] + "…") if len(t) > 60 else (t or "New conversation")


def handle_message(
    db: Session,
    user_id: int,
    message: str,
    conversation_id: str | None,
    weights: dict[str, float] | None,
    settings: Settings,
    provider: LLMProvider,
) -> tuple[Conversation, Message, Message, list[AgentAction], AgentResult]:
    text = sanitize_user_text(message)
    if not text:
        raise ValidationFailed("Message cannot be empty")
    if conversation_id:
        conv = get_conversation(db, user_id, conversation_id)
        if len(conv.messages) >= MAX_MESSAGES_PER_CONVERSATION:
            raise ValidationFailed("This conversation is full. Please start a new one.")
    else:
        conv = Conversation(id=str(uuid.uuid4()), user_id=user_id, title=_title(text), context={})
        db.add(conv)
        db.flush()

    user_msg = Message(conversation_id=conv.id, role="user", content=text, payload={})
    db.add(user_msg)
    db.flush()

    prefs = pref_service.as_dicts(pref_service.list_preferences(db, user_id))
    deps = AgentDeps(
        tools=ToolBox(DbCatalog(db), on_retry=db.rollback),
        provider=provider,
        max_item_quantity=settings.max_item_quantity,
        deadline_seconds=settings.agent_deadline_seconds,
    )
    result = run_agent(text, deps, context=dict(conv.context or {}), saved_preferences=prefs, weights=weights)

    # Update short-term working memory.
    if result.response_type == "reset":
        conv.context = {}
    elif result.response_type in {"recommendations", "no_results"} and result.intent is not None:
        shown = result.recommendations or result.alternatives
        conv.context = build_context(
            result.intent, [c["product_id"] for c in shown], shown[0]["price_inr"] if shown else None
        )
    elif result.response_type == "comparison" and result.recommendations:
        ctx = dict(conv.context or {})
        ctx["last_recommendation_ids"] = [c["product_id"] for c in result.recommendations]
        conv.context = ctx

    actions = action_service.persist_actions(db, user_id, conv.id, result.proposed_actions, settings)
    payload = result.payload()
    payload["action_ids"] = [a.id for a in actions]
    assistant = Message(conversation_id=conv.id, role="assistant", content=result.summary, payload=payload)
    db.add(assistant)
    conv.updated_at = utcnow()
    db.commit()
    db.refresh(conv)
    return conv, user_msg, assistant, actions, result
