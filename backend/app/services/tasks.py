"""Persistent agent tasks: planning, leased execution, retries, cancellation and resume.

A task is a server-planned sequence of steps. Each step calls a read-only tool from the Phase 1
registry (or the built-in, deterministic "summarize" step). Every state change is committed
immediately, so a crash loses at most the step in flight:

* **Leases.** A runner claims a task (`locked_by`, `locked_until`) under a row lock. A task whose
  lease has expired was interrupted (restart, deploy, crash) and can be resumed.
* **No duplicated work.** Finished steps (`succeeded`, `skipped`) are never executed again; their
  stored output is reused. A step's arguments are frozen when it first starts, so a resumed step
  replays exactly the same call. Attempts are recorded before each call.
* **Retries.** Transient tool failures (`temporarily_unavailable`) are retried with exponential
  backoff up to the step's `max_attempts`. If they keep failing the task fails as *retryable* and
  can be resumed; permanent failures are not retryable.
* **Approval gating.** Steps come only from the server-side planner, never from the client, and the
  runner allows the "read" permission only. Phase 2 has no step that changes a cart or places an
  order; such steps will have to go through the existing pending-action approval flow.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.agents.guardrails import sanitize_user_text
from app.agents.parser import RequestParser, strip_budget_phrases
from app.agents.planner import (
    AgentGoal,
    Clarify,
    ConstraintReport,
    Final,
    Observation,
    Planner,
    PlannerState,
    RuleBasedPlanner,
    ToolCall,
    check_decision,
    derive_intent,
)
from app.agents.tool_registry import (
    RETRYABLE_CODES,
    TaskConstraints,
    ToolContext,
    ToolRegistry,
    build_task_registry,
)
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, RateLimited, ValidationFailed
from app.models import AgentTask, AgentTaskStep
from app.models.task import FINISHED_STEP_STATUSES, TERMINAL_TASK_STATUSES
from app.services.specs import CATEGORY_LABELS, format_inr

logger = logging.getLogger("agentcart.tasks")

TaskKind = Literal["product_research", "shopping_agent"]
ACTIVE_STATUSES = ("queued", "running")
SUMMARIZE = "summarize"  # built-in step, not a registry tool
# Built-in, non-executable trace steps that end a planning loop.
FINAL_STEP_TOOLS = ("final", "clarify")
# Permissions offered to the planning loop: read tools always; "propose" (pending, approval-gated cart
# proposals) only when the user opted in with `allow_proposals`. There is no permission that changes the cart.
LOOP_PERMISSIONS: frozenset[str] = frozenset({"read"})
READ_ONLY: frozenset[str] = frozenset({"read"})


def loop_permissions(goal: AgentGoal, settings: Settings) -> frozenset[str]:
    if goal.allow_proposals and settings.task_max_proposals > 0:
        return LOOP_PERMISSIONS | {"propose"}
    return LOOP_PERMISSIONS


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


# ------------------------------------------------------------------ task kinds and planning
class ProductResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    query: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = None
    max_price_inr: int | None = Field(default=None, ge=1, le=10_000_000)
    min_rating: float | None = Field(default=None, ge=0, le=5)
    in_stock: bool = True
    compare_top: int = Field(default=3, ge=2, le=4)
    include_cart: bool = True

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str | None) -> str | None:
        if v is not None and v not in CATEGORY_LABELS:
            raise ValueError("unknown category")
        return v

    @model_validator(mode="after")
    def _needs_a_subject(self) -> ProductResearchInput:
        if self.query is None and self.category is None:
            raise ValueError("give a query or a category")
        return self


INPUT_MODELS: dict[str, type[BaseModel]] = {"product_research": ProductResearchInput, "shopping_agent": AgentGoal}


@dataclass(frozen=True)
class PlannedStep:
    name: str
    tool: str


def plan_steps(kind: str, inp: BaseModel) -> list[PlannedStep]:
    """Server-side plan. Clients can choose the task kind and its typed input, never the steps."""
    if kind == "product_research":
        assert isinstance(inp, ProductResearchInput)
        steps = [PlannedStep("search", "search_catalog"), PlannedStep("compare", "compare_products")]
        if inp.include_cart:
            steps.append(PlannedStep("cart", "read_cart"))
        steps.append(PlannedStep("summarize", SUMMARIZE))
        return steps
    if kind == "shopping_agent":
        return []  # planned one decision at a time by the planning loop
    raise ValidationFailed("Unknown task kind")


def goal_for(kind: str, inp: BaseModel) -> str:
    if isinstance(inp, AgentGoal):
        return f"Agent: {inp.goal}"[:300]
    assert isinstance(inp, ProductResearchInput)
    subject = CATEGORY_LABELS.get(inp.category or "", "").lower() or "products"
    parts = [f"Research {subject}"]
    if inp.query:
        parts.append(f"matching “{inp.query}”")
    if inp.max_price_inr is not None:
        parts.append(f"under {format_inr(inp.max_price_inr)}")
    return " ".join(parts)[:300]


def validate_input(kind: str, raw: Any) -> BaseModel:
    model = INPUT_MODELS.get(kind)
    if model is None:
        raise ValidationFailed("Unknown task kind")
    if not isinstance(raw, dict):
        raise ValidationFailed("Task input must be an object")
    try:
        inp = model.model_validate(raw)
    except ValidationError as exc:
        errors = [
            {"loc": [str(p) for p in e["loc"]], "msg": e["msg"]}
            for e in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        raise ValidationFailed("Invalid task input", details={"errors": errors}) from exc
    if isinstance(inp, ProductResearchInput):
        terms = _research_terms(inp.model_dump())
        floor, cap = terms["min_price_inr"], terms["max_price_inr"]
        if floor is not None and cap is not None and floor > cap:
            # Never drop either limit silently: nothing can satisfy both.
            raise ValidationFailed(
                "The price in “Looking for” conflicts with the maximum price",
                details={"errors": [{"loc": ["query"], "msg": "minimum price is above the maximum price"}]},
            )
    return inp


# ------------------------------------------------------------------ queries and API-facing operations
def get_owned(db: Session, user_id: int, task_id: str) -> AgentTask:
    task = db.get(AgentTask, task_id)
    if task is None or task.user_id != user_id:
        raise NotFoundError("Task not found")  # never reveal another user's task
    return task


def list_tasks(db: Session, user_id: int, limit: int = 50) -> list[AgentTask]:
    stmt = (
        select(AgentTask)
        .where(AgentTask.user_id == user_id)
        .options(selectinload(AgentTask.proposals))  # one query for every listed task's proposals
        .order_by(AgentTask.created_at.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def is_interrupted(task: AgentTask, now: datetime | None = None) -> bool:
    """Running, but nobody holds a live lease: the runner died mid-task."""
    until = _aware(task.locked_until)
    return task.status == "running" and (until is None or until <= (now or _now()))


def is_stalled(task: AgentTask, *, stale_after_seconds: int, now: datetime | None = None) -> bool:
    """Queued, and nothing has happened for longer than a runner lease: its scheduled run was lost
    (e.g. a deploy right after it was created or resumed). A freshly queued task is about to run."""
    changed = _aware(task.updated_at) or _aware(task.created_at)
    return (
        task.status == "queued"
        and changed is not None
        and changed <= (now or _now()) - timedelta(seconds=stale_after_seconds)
    )


def is_resumable(task: AgentTask, *, stale_after_seconds: int, now: datetime | None = None) -> bool:
    if task.status == "queued":
        return is_stalled(task, stale_after_seconds=stale_after_seconds, now=now)
    if is_interrupted(task, now):
        return True
    return task.status == "failed" and bool((task.error or {}).get("retryable"))


def create_task(
    db: Session, user_id: int, kind: str, raw_input: Any, idempotency_key: str | None, settings: Settings
) -> tuple[AgentTask, bool]:
    """Returns (task, created). Re-sending the same idempotency key returns the original task."""
    inp = validate_input(kind, raw_input)
    normalised = inp.model_dump(mode="json")
    if idempotency_key:
        existing = db.scalar(
            select(AgentTask).where(AgentTask.user_id == user_id, AgentTask.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if existing.kind != kind or existing.input != normalised:
                raise ConflictError("This idempotency key was already used for a different task")
            return existing, False
    active = db.scalar(
        select(func.count())
        .select_from(AgentTask)
        .where(AgentTask.user_id == user_id, AgentTask.status.in_(ACTIVE_STATUSES))
    )
    if (active or 0) >= settings.task_max_active_per_user:
        raise RateLimited(
            f"You already have {active} active tasks. Wait for one to finish or cancel one.",
            details={"active": active, "limit": settings.task_max_active_per_user},
        )
    plan = plan_steps(kind, inp)
    task = AgentTask(
        id=str(uuid.uuid4()),
        user_id=user_id,
        kind=kind,
        goal=goal_for(kind, inp),
        input=normalised,
        status="queued",
        progress=0,
        current_step=0,
        total_steps=len(plan),
        idempotency_key=idempotency_key,
    )
    task.steps = [
        AgentTaskStep(position=i, name=s.name, tool=s.tool, max_attempts=settings.task_step_max_attempts)
        for i, s in enumerate(plan)
    ]
    db.add(task)
    try:
        db.commit()
    except IntegrityError:
        # Two requests with the same key raced; the other one won.
        db.rollback()
        if idempotency_key:
            return create_task(db, user_id, kind, raw_input, idempotency_key, settings)
        raise
    return task, True


def request_cancel(db: Session, user_id: int, task_id: str) -> AgentTask:
    task = get_owned(db, user_id, task_id)
    if task.status in TERMINAL_TASK_STATUSES:
        raise ConflictError(f"This task is already {task.status}", details={"status": task.status})
    task.cancel_requested = True
    if task.status == "queued" or is_interrupted(task):
        # Nobody is executing it: cancel right away.
        _finish_cancelled(task)
    # Otherwise the live runner stops at the next step boundary.
    db.commit()
    return task


def prepare_resume(db: Session, user_id: int, task_id: str, settings: Settings) -> AgentTask:
    task = get_owned(db, user_id, task_id)
    if task.cancel_requested or not is_resumable(task, stale_after_seconds=settings.task_lease_seconds):
        reason = "running" if task.status == "running" else task.status
        raise ConflictError("This task can't be resumed", details={"status": task.status, "reason": reason})
    if task.status == "failed":
        for step in task.steps:
            if step.status == "failed":
                step.status, step.attempts, step.error = "queued", 0, None
        task.status, task.error, task.finished_at = "queued", None, None
    elif is_interrupted(task):
        task.status, task.locked_by, task.locked_until = "queued", None, None
    # Restart the grace period: a just-resumed task is not "stalled" again until its new run is lost too.
    task.updated_at = _now()
    db.commit()
    return task


def _finish_cancelled(task: AgentTask) -> None:
    now = _now()
    for step in task.steps:
        if step.status not in FINISHED_STEP_STATUSES and step.status != "failed":
            step.status = "cancelled"
            step.finished_at = step.finished_at or now
    task.status = "cancelled"
    task.locked_by = task.locked_until = None
    task.finished_at = now


# ------------------------------------------------------------------ execution
StepOutcome = tuple[bool, dict[str, Any] | None, dict[str, Any] | None]  # ok, output, error


class _LeaseLost(Exception):
    """This runner's lease expired and another runner claimed the task."""


class TaskRunner:
    """Executes tasks. One runner call drives one task until it finishes, fails, is cancelled, or
    loses its lease. Safe to call again on the same task: it only claims a task that nobody holds."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        registry_factory: Callable[[], ToolRegistry] = build_task_registry,
        sleep: Callable[[float], None] = time.sleep,
        planner_factory: Callable[[], Planner] = RuleBasedPlanner,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.registry_factory = registry_factory
        self.sleep = sleep
        self.planner_factory = planner_factory  # offline RuleBasedPlanner by default: no provider, no API key
        self.clock = clock

    def run(self, task_id: str) -> str | None:
        """Returns the task's status when this runner let go of it, or None if it couldn't claim it."""
        runner_id = str(uuid.uuid4())
        try:
            with self.session_factory() as db:
                task = self._claim(db, task_id, runner_id)
                if task is None:
                    return None
                try:
                    return self._drive(db, task, runner_id)
                except _LeaseLost:
                    # Another runner owns the task now: stop without writing anything.
                    db.rollback()
                    logger.warning("task %s: lease lost during a retry, stopping", task_id)
                    return None
        except OperationalError:
            # The database itself is unavailable: the lease expires and the task becomes resumable.
            logger.warning("task %s interrupted by a database failure", task_id, exc_info=True)
            return "interrupted"
        except Exception:
            logger.exception("task %s runner crashed", task_id)
            self._mark_crashed(task_id, runner_id)
            return "failed"

    # -- lease handling
    def _lease_until(self) -> datetime:
        return _now() + timedelta(seconds=self.settings.task_lease_seconds)

    def _claim(self, db: Session, task_id: str, runner_id: str) -> AgentTask | None:
        task = db.get(AgentTask, task_id, with_for_update=True)
        if task is None or task.status not in ACTIVE_STATUSES:
            db.rollback()
            return None
        held = _aware(task.locked_until)
        if task.locked_by is not None and held is not None and held > _now():
            db.rollback()  # someone else is running it
            return None
        if task.cancel_requested:
            _finish_cancelled(task)
            db.commit()
            return None
        task.locked_by, task.locked_until = runner_id, self._lease_until()
        task.status = "running"
        task.attempt += 1
        task.started_at = task.started_at or _now()
        task.error = None
        db.commit()
        return task

    def _still_ours(self, db: Session, task: AgentTask, runner_id: str) -> bool:
        """Re-read the task under a row lock; renew the lease if we still hold it."""
        db.refresh(task, with_for_update=True)
        if task.locked_by != runner_id:
            db.rollback()
            return False
        task.locked_until = self._lease_until()
        return True

    # -- the loop
    def _drive(self, db: Session, task: AgentTask, runner_id: str) -> str:
        if task.kind == "shopping_agent":
            return self._drive_loop(db, task, runner_id)
        registry = self.registry_factory()
        outputs = {s.name: s.output for s in task.steps if s.status == "succeeded"}
        for step in task.steps:
            if not self._still_ours(db, task, runner_id):
                logger.warning("task %s: lease lost, stopping", task.id)
                return task.status
            if task.cancel_requested:
                _finish_cancelled(task)
                db.commit()
                return "cancelled"
            if step.status in FINISHED_STEP_STATUSES:
                continue  # done in an earlier run: never repeated
            if step.args is None:
                resolved = _resolve_args(step, task.input, outputs)
                if resolved is None:
                    step.status, step.output = "skipped", {"reason": _skip_reason(step, task.input, outputs)}
                    step.finished_at = _now()
                    self._advance(task, step)
                    db.commit()
                    continue
                step.args = resolved
            ok, output, error = self._run_step_with_retries(db, task, step, registry, outputs, runner_id)
            if not ok:
                assert error is not None
                return self._fail(db, task, step, error)
            outputs[step.name] = output
        task.result = outputs.get("summarize") or {}
        task.status, task.progress = "succeeded", 100
        task.current_step = task.total_steps
        task.locked_by = task.locked_until = None
        task.finished_at = _now()
        db.commit()
        return "succeeded"

    def _run_step_with_retries(
        self,
        db: Session,
        task: AgentTask,
        step: AgentTaskStep,
        registry: ToolRegistry,
        outputs: dict[str, dict[str, Any] | None],
        runner_id: str,
        constraints: TaskConstraints | None = None,
        permissions: frozenset[str] = READ_ONLY,
    ) -> StepOutcome:
        while True:
            step.status = "running"
            step.attempts += 1  # recorded before the call: a crash mid-call still counts
            step.started_at = step.started_at or _now()
            task.locked_until = self._lease_until()
            db.commit()
            ok, output, error = self._execute(db, task, step, registry, outputs, constraints, permissions)
            if ok:
                step.status, step.output, step.error = "succeeded", output, None
                step.finished_at = _now()
                self._advance(task, step)
                db.commit()
                return ok, output, None
            assert error is not None
            step.error = error
            if error.get("retryable") and step.attempts < step.max_attempts:
                step.status = "queued"
                db.commit()
                self.sleep(self.settings.task_retry_backoff_seconds * 2 ** (step.attempts - 1))
                if not self._still_ours(db, task, runner_id):
                    raise _LeaseLost
                continue
            return False, None, error

    def _execute(
        self,
        db: Session,
        task: AgentTask,
        step: AgentTaskStep,
        registry: ToolRegistry,
        outputs: dict[str, dict[str, Any] | None],
        constraints: TaskConstraints | None = None,
        permissions: frozenset[str] = READ_ONLY,
    ) -> StepOutcome:
        if step.tool == SUMMARIZE:
            return True, _summarize(task.input, outputs), None
        ctx = ToolContext(
            db=db,
            settings=self.settings,
            user_id=task.user_id,
            allowed_permissions=permissions,  # read only, unless this agent task opted in to proposals
            constraints=constraints,  # the task's own requirements; never from step arguments
            task_id=task.id,  # a proposal is tied to this step: a resumed step can't propose twice
            task_step_id=step.id,
        )
        result = registry.execute(step.tool, step.args or {}, ctx)
        if result.ok:
            return True, result.data, None
        assert result.error is not None
        return (
            False,
            None,
            {
                "code": result.error.code,
                "message": result.error.message,
                "retryable": result.error.code in RETRYABLE_CODES,
            },
        )

    # -- the planning loop (shopping_agent)
    def _drive_loop(self, db: Session, task: AgentTask, runner_id: str) -> str:
        """Bounded plan → validate → persist → execute loop.

        Each decision is validated (offered tool, grounded final, arguments against the tool's own schema)
        and persisted as a step *before* it runs, so a resumed task finishes the decided step instead of
        asking the planner again. Finished steps are never repeated. Hard constraints come only from the
        deterministic intent, never from the planner.
        """
        s = self.settings
        registry = self.registry_factory()
        planner = self.planner_factory()
        goal = AgentGoal.model_validate(task.input)
        permissions = loop_permissions(goal, s)
        tools = [
            spec.schema()
            for name in registry.names()
            if (spec := registry.get(name)) is not None and spec.permission in permissions
        ]
        intent, questions = derive_intent(goal, max_item_quantity=s.max_item_quantity)
        constraints = TaskConstraints.from_intent(intent)
        deadline = self.clock() + timedelta(seconds=s.task_deadline_seconds)
        state: dict[str, Any] = {
            "planner": planner.name,
            "iterations": 0,
            "tool_calls": 0,
            "invalid_decisions": 0,
            "runs": 0,
            **(task.state or {}),
        }
        state["runs"] = int(state["runs"]) + 1
        state["deadline_at"] = deadline.isoformat()  # per run; the counters are totals across runs
        task.state = state
        db.commit()

        while True:
            if not self._still_ours(db, task, runner_id):
                logger.warning("task %s: lease lost, stopping", task.id)
                return task.status
            if task.cancel_requested:
                _finish_cancelled(task)
                db.commit()
                return "cancelled"
            pending = next((st for st in task.steps if st.status in ("queued", "running")), None)
            if pending is None:
                limit = self._loop_limit(task, deadline)
                if limit is not None:
                    return self._stop_loop(db, task, *limit)
                observations = _observations(task.steps)
                pstate = PlannerState(goal=goal, intent=intent, questions=questions, observations=observations)
                self._count(task, "iterations")
                try:
                    decision = planner.decide(pstate, tools)
                except Exception:
                    # Never surface planner internals (or a provider's message) to the user.
                    logger.exception("task %s: planner %s failed", task.id, planner.name)
                    return self._stop_loop(db, task, "planner_error", "The planner failed to decide a next step", False)
                problem = check_decision(decision, pstate, tools) or _argument_problem(decision, registry)
                if problem is not None:
                    self._record_rejected(db, task, decision, problem, planner.name)
                    if self._state_int(task, "invalid_decisions") > s.task_max_invalid_decisions:
                        return self._stop_loop(
                            db, task, "invalid_decisions", "The planner kept proposing invalid steps", False
                        )
                    continue
                if not isinstance(decision, ToolCall):
                    return self._finish_loop(db, task, decision, observations, constraints, planner.name)
                if self._state_int(task, "tool_calls") >= s.task_max_tool_calls:
                    return self._stop_loop(
                        db, task, "limit_reached", f"Stopped after {s.task_max_tool_calls} tool calls", False
                    )
                pending = self._append_step(db, task, decision, planner.name)
            ok, _output, error = self._run_step_with_retries(
                db, task, pending, registry, {}, runner_id, constraints=constraints, permissions=permissions
            )
            if not ok:
                assert error is not None
                return self._fail(db, task, pending, error)
            self._loop_progress(task)
            db.commit()

    @staticmethod
    def _state_int(task: AgentTask, key: str) -> int:
        return int((task.state or {}).get(key, 0))

    def _count(self, task: AgentTask, key: str) -> int:
        state = dict(task.state or {})
        state[key] = int(state.get(key, 0)) + 1
        task.state = state  # reassigned so the JSON column is marked dirty
        return int(state[key])

    def _loop_limit(self, task: AgentTask, deadline: datetime) -> tuple[str, str, bool] | None:
        s = self.settings
        if self.clock() >= deadline:
            # Time is per run: a resumed run gets a fresh budget and continues where this one stopped.
            return "deadline_exceeded", f"Stopped after {s.task_deadline_seconds:g} seconds", True
        if self._state_int(task, "iterations") >= s.task_max_iterations:
            return "limit_reached", f"Stopped after {s.task_max_iterations} planning steps", False
        return None

    def _loop_progress(self, task: AgentTask) -> None:
        task.current_step = sum(1 for st in task.steps if st.status in FINISHED_STEP_STATUSES)
        task.total_steps = len(task.steps)
        iterations = self._state_int(task, "iterations")
        task.progress = min(99, round(100 * iterations / max(1, self.settings.task_max_iterations)))

    def _decision_record(self, task: AgentTask, decision: ToolCall | Final | Clarify, planner: str) -> dict[str, Any]:
        return {
            "planner": planner,
            "iteration": self._state_int(task, "iterations"),
            "kind": decision.kind,
            "rationale": decision.rationale if isinstance(decision, ToolCall) else "",
        }

    def _append_step(self, db: Session, task: AgentTask, decision: ToolCall, planner: str) -> AgentTaskStep:
        """Persist the decision as a queued step and commit before executing it."""
        step = AgentTaskStep(
            position=len(task.steps),
            name=decision.tool,
            tool=decision.tool,
            args=dict(decision.args),  # frozen: a resumed step replays exactly this call
            status="queued",
            attempts=0,
            max_attempts=self.settings.task_step_max_attempts,
            decision=self._decision_record(task, decision, planner),
        )
        task.steps.append(step)
        task.total_steps = len(task.steps)
        self._count(task, "tool_calls")
        db.commit()
        return step

    def _record_rejected(
        self, db: Session, task: AgentTask, decision: ToolCall | Final | Clarify, code: str, planner: str
    ) -> None:
        """Keep a refused decision in the trace as a finished (`skipped`) step: never executed, never resumed."""
        self._count(task, "invalid_decisions")
        label = decision.tool if isinstance(decision, ToolCall) else decision.kind
        now = _now()
        task.steps.append(
            AgentTaskStep(
                position=len(task.steps),
                name=f"rejected:{label}"[:60],
                tool=label[:60],
                args=dict(decision.args) if isinstance(decision, ToolCall) else None,
                status="skipped",
                attempts=0,
                max_attempts=1,
                error={"code": code, "message": _REJECTION_MESSAGES.get(code, "Decision rejected"), "retryable": False},
                decision={**self._decision_record(task, decision, planner), "rejected": True},
                started_at=now,
                finished_at=now,
            )
        )
        task.total_steps = len(task.steps)
        db.commit()

    def _finish_loop(
        self,
        db: Session,
        task: AgentTask,
        decision: Final | Clarify,
        observations: tuple[Observation, ...],
        constraints: TaskConstraints,
        planner: str,
    ) -> str:
        result = _compose_result(decision, observations, constraints)
        now = _now()
        task.steps.append(
            AgentTaskStep(
                position=len(task.steps),
                name=decision.kind,
                tool=decision.kind,
                args=None,
                status="succeeded",
                attempts=0,
                max_attempts=1,
                output=result,
                decision=self._decision_record(task, decision, planner),
                started_at=now,
                finished_at=now,
            )
        )
        task.result = result
        task.status, task.progress = "succeeded", 100
        task.total_steps = task.current_step = len(task.steps)
        task.locked_by = task.locked_until = None
        task.finished_at = now
        db.commit()
        return "succeeded"

    def _stop_loop(self, db: Session, task: AgentTask, code: str, message: str, retryable: bool) -> str:
        task.status = "failed"
        task.error = {"code": code, "message": message, "retryable": retryable}
        task.locked_by = task.locked_until = None
        task.finished_at = _now()
        db.commit()
        return "failed"

    def _advance(self, task: AgentTask, step: AgentTaskStep) -> None:
        task.current_step = step.position + 1
        task.progress = min(99, round(100 * task.current_step / max(1, task.total_steps)))

    def _fail(self, db: Session, task: AgentTask, step: AgentTaskStep, error: dict[str, Any]) -> str:
        step.status, step.error = "failed", error
        step.finished_at = _now()
        task.status = "failed"
        task.error = {**error, "step": step.name}
        task.locked_by = task.locked_until = None
        task.finished_at = _now()
        db.commit()
        return "failed"

    def _mark_crashed(self, task_id: str, runner_id: str) -> None:
        try:
            with self.session_factory() as db:
                task = db.get(AgentTask, task_id, with_for_update=True)
                if task is not None and task.locked_by == runner_id:
                    task.status = "failed"
                    task.error = {
                        "code": "internal_error",
                        "message": "The task failed unexpectedly",
                        "retryable": True,
                    }
                    task.locked_by = task.locked_until = None
                    task.finished_at = _now()
                    db.commit()
        except Exception:
            logger.exception("could not record crash of task %s", task_id)


# ------------------------------------------------------------------ product_research step logic
def _resolve_args(
    step: AgentTaskStep, inp: dict[str, Any], outputs: dict[str, dict[str, Any] | None]
) -> dict[str, Any] | None:
    """Arguments for a step, or None when the step should be skipped."""
    if step.name == "search":
        terms = _research_terms(inp)
        args: dict[str, Any] = {
            "in_stock": inp.get("in_stock", True),
            "sort": "relevance" if terms["query"] else "rating",
            "page_size": 12,
        }
        for key in ("query", "min_price_inr", "max_price_inr"):
            if terms[key] is not None:
                args[key] = terms[key]
        for key in ("category", "min_rating"):
            if inp.get(key) is not None:
                args[key] = inp[key]
        return args
    if step.name == "compare":
        items = (outputs.get("search") or {}).get("items") or []
        ids = [p["id"] for p in items[: int(inp.get("compare_top", 3))]]
        return {"product_ids": ids} if len(ids) >= 2 else None
    if step.name in ("cart", "summarize"):
        return {}
    raise ValidationFailed(f"Unknown step {step.name!r}")


def _research_terms(inp: dict[str, Any]) -> dict[str, Any]:
    """Search keywords and price bounds for a research task.

    A budget typed into the query ("laptop under ₹60,000") is read by the deterministic parser, as in
    chat, instead of being searched for as words: every keyword must match, and no product text contains
    "under 60000". A parsed budget can only narrow an explicit `max_price_inr`, never widen it, and both
    limits are kept as typed: a minimum above the maximum is rejected when the task is created.
    """
    query, cap = inp.get("query"), inp.get("max_price_inr")
    floor: int | None = None
    if query:
        intent = RequestParser().parse(sanitize_user_text(query))
        if intent.budget_max is not None:
            cap = intent.budget_max if cap is None else min(cap, intent.budget_max)
        floor = intent.budget_min
        query = _product_words(strip_budget_phrases(query)) or None
    return {"query": query, "min_price_inr": floor, "max_price_inr": cap}


# How people phrase a request ("I need a …", "show me a good …"), not properties of a product. Every search
# keyword must match, so leaving these in finds nothing ("need" is in no product's text).
_REQUEST_WORDS = frozenset(
    [
        "i",
        "i'm",
        "im",
        "me",
        "my",
        "we",
        "us",
        "you",
        "please",
        "pls",
        "need",
        "needs",
        "want",
        "wants",
        "wanted",
        "would",
        "like",
        "looking",
        "look",
        "searching",
        "show",
        "find",
        "get",
        "buy",
        "purchase",
        "recommend",
        "suggest",
        "some",
        "something",
        "any",
        "good",
        "great",
        "nice",
        "decent",
        "best",
        "a",
        "an",
        "the",
        "for",
        "to",
        "can",
        "could",
    ]
)


def _product_words(text: str) -> str:
    """`text` without request phrasing, keeping product words ("gaming laptop", "16gb", "noise cancelling")."""
    return " ".join(w for w in text.split() if w.lower().strip(".,!?;:'\"") not in _REQUEST_WORDS)


def _skip_reason(step: AgentTaskStep, inp: dict[str, Any], outputs: dict[str, dict[str, Any] | None]) -> str:
    if step.name == "compare":
        n = len((outputs.get("search") or {}).get("items") or [])
        return f"Only {n} matching product{'s' if n != 1 else ''}; nothing to compare"
    return "Not needed"


def _summarize(inp: dict[str, Any], outputs: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """Deterministic summary built only from stored step outputs (no LLM, no new data)."""
    search = outputs.get("search") or {}
    items = search.get("items") or []
    top = [
        {k: p[k] for k in ("id", "name", "brand", "category", "price_inr", "rating", "in_stock")}
        for p in items[: int(inp.get("compare_top", 3))]
    ]
    summary: dict[str, Any] = {"matches": int(search.get("total") or 0), "top": top, "best": {}}
    comparison = outputs.get("compare")
    if comparison:
        names = [c["name"] for c in comparison.get("columns", [])]
        for row in comparison.get("rows", []):
            if row.get("key") in ("price_inr", "rating") and row.get("best"):
                summary["best"][row["key"]] = [names[i] for i in row["best"] if i < len(names)]
    cart = outputs.get("cart")
    if cart is not None:
        summary["cart"] = {"item_count": cart.get("item_count", 0), "total_inr": cart.get("total_inr", 0)}
    terms = _research_terms(inp)
    cap, floor = terms["max_price_inr"], terms["min_price_inr"]
    summary["all_within_budget"] = all(
        (cap is None or p["price_inr"] <= cap) and (floor is None or p["price_inr"] >= floor) for p in top
    )
    return summary


# ------------------------------------------------------------------ planning-loop helpers (shopping_agent)
_REJECTION_MESSAGES = {
    "unknown_tool": "The planner chose a tool that isn't available to this task",
    "invalid_arguments": "The planner's arguments don't match the tool's schema",
    "ungrounded_final": "The planner named products that no constraint check passed",
    "ungrounded_proposal": "The planner proposed a product that no constraint check passed",
}


def _argument_problem(decision: ToolCall | Final | Clarify, registry: ToolRegistry) -> str | None:
    """Validate a tool call's arguments against the registered input schema before anything runs."""
    if not isinstance(decision, ToolCall):
        return None
    spec = registry.get(decision.tool)
    if spec is None:
        return "unknown_tool"
    try:
        spec.input_model.model_validate(decision.args)
    except ValidationError:
        return "invalid_arguments"
    return None


def _observations(steps: list[AgentTaskStep]) -> tuple[Observation, ...]:
    """What the planner has learned so far: executed tool steps only (not rejections or trace steps)."""
    out: list[Observation] = []
    for st in steps:
        if st.tool in FINAL_STEP_TOOLS or (st.decision or {}).get("rejected"):
            continue
        if st.status == "succeeded":
            out.append(Observation(tool=st.tool, args=dict(st.args or {}), ok=True, data=st.output))
        elif st.status == "failed":
            out.append(
                Observation(tool=st.tool, args=dict(st.args or {}), ok=False, error_code=(st.error or {}).get("code"))
            )
    return tuple(out)


def _latest_data(observations: tuple[Observation, ...], tool: str) -> dict[str, Any] | None:
    return next((o.data for o in reversed(observations) if o.tool == tool and o.ok), None)


def _compose_result(
    decision: Final | Clarify, observations: tuple[Observation, ...], constraints: TaskConstraints
) -> dict[str, Any]:
    """The task result, built only from stored tool outputs: names, prices, ratings and stock are the
    catalogue's, and the products are ones a constraint check passed (enforced by `check_decision`)."""
    search = _latest_data(observations, "search_catalog") or {}
    items = {int(p["id"]): p for p in search.get("items") or []}
    report_data = _latest_data(observations, "evaluate_constraints")
    report = ConstraintReport.model_validate(report_data) if report_data else None
    result: dict[str, Any] = {
        "type": "clarification",
        "question": None,
        "products": [],
        "notes": "",
        "requirements": report.constraints if report and report.constraints else constraints.labels(),
        "candidates": len(items),
        "eligible": sum(1 for r in report.results if r.passes) if report else 0,
    }
    if isinstance(decision, Clarify):
        result["question"] = decision.question
        return result
    result["products"] = [
        {k: items[pid][k] for k in ("id", "name", "brand", "category", "price_inr", "rating", "in_stock")}
        for pid in decision.product_ids
        if pid in items
    ]
    result["type"] = "recommendations" if result["products"] else "no_match"
    result["notes"] = decision.notes
    cart = _latest_data(observations, "read_cart")
    if cart is not None:
        result["cart"] = {"item_count": cart.get("item_count", 0), "total_inr": cart.get("total_inr", 0)}
    # Pending cart proposals, as proposed. Their live status (approved, rejected, expired …) is the task's
    # `proposals` in the API; nothing is added to the cart until the user approves one.
    result["proposals"] = [
        {k: o.data[k] for k in ("action_id", "product_id", "product_name", "quantity", "unit_price_inr", "summary")}
        for o in observations
        if o.tool == "propose_add_to_cart" and o.ok and o.data
    ]
    return result
