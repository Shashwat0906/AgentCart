"""Conversations, messages, pending agent actions and saved preferences."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="New conversation")
    # Short-term working memory: the last validated intent and recommendation ids.
    # Deliberately separate from the persistent `preferences` table.
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    # Structured agent output: recommendations, comparison, questions, trace …
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AgentAction(Base):
    """An action proposed by the agent. Nothing happens until the owner approves it."""

    __tablename__ = "agent_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(32))  # add_to_cart | save_preference
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType)
    summary: Mapped[str] = mapped_column(String(300))
    # pending | executed | rejected | failed | expired | superseded
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()
    decided_at: Mapped[datetime | None] = mapped_column(default=None)


class Preference(Base):
    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("user_id", "kind", "category", "value", name="uq_preferences_entry"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # preferred_brand | avoided_brand | budget_max | use_case | note
    kind: Mapped[str] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    value: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(16), default="user")  # user | agent
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
