"""Persistent agent tasks: multi-step work that survives restarts and can be resumed safely."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, utcnow

if TYPE_CHECKING:
    from app.models.agent import AgentAction

# "awaiting_approval" is reserved for later phases (approval-gated cart/checkout steps); Phase 2 never sets it.
TASK_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "awaiting_approval")
TERMINAL_TASK_STATUSES = ("succeeded", "failed", "cancelled")
STEP_STATUSES = ("queued", "running", "succeeded", "failed", "skipped", "cancelled")
FINISHED_STEP_STATUSES = ("succeeded", "skipped")  # never executed again on resume


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_agent_tasks_user_idempotency_key"),
        CheckConstraint(_in("status", TASK_STATUSES), name="status_valid"),
        CheckConstraint("progress >= 0 AND progress <= 100", name="progress_range"),
        CheckConstraint("current_step >= 0 AND total_steps >= 0", name="steps_nonnegative"),
        CheckConstraint("attempt >= 0", name="attempt_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    goal: Mapped[str] = mapped_column(String(300))
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, default=None)
    # {"code", "message", "retryable"} when the task failed
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, default=None)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True, default=None)
    # Lease: the runner holding it may execute steps until locked_until; an expired lease means the
    # runner died (restart, deploy, crash) and the task can be resumed.
    locked_by: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)
    locked_until: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    attempt: Mapped[int] = mapped_column(Integer, default=0)  # number of runs started
    # Planning-loop bookkeeping (shopping_agent tasks): planner name, iteration / tool-call / invalid-decision
    # counters and the current run's deadline. NULL for fixed-plan tasks.
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)

    steps: Mapped[list[AgentTaskStep]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="AgentTaskStep.position"
    )
    # Cart proposals this task made (S4). Read-only: actions are created by the propose tool and decided
    # through the approval API, never through the task.
    proposals: Mapped[list[AgentAction]] = relationship(viewonly=True, order_by="AgentAction.created_at")


class AgentTaskStep(Base):
    __tablename__ = "agent_task_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "position", name="uq_agent_task_steps_task_position"),
        CheckConstraint(_in("status", STEP_STATUSES), name="status_valid"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
        CheckConstraint("position >= 0", name="position_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(60))
    tool: Mapped[str] = mapped_column(String(60))  # registry tool name, or "summarize" (built in)
    # Resolved when the step first starts and then frozen, so a resumed step replays the same call.
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, default=None)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, default=None)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, default=None)
    # Audit record of the planner decision that created this step (planner, iteration, rationale, and
    # `rejected` + code when the decision was refused). NULL for fixed-plan steps.
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True, default=None)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True, default=None)

    task: Mapped[AgentTask] = relationship(back_populates="steps")
