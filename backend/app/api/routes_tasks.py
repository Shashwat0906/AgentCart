"""Persistent agent tasks API. Every endpoint is owner-only: other users' tasks are 404."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, write_limit
from app.core.config import Settings, get_settings
from app.db.session import get_db, get_sessionmaker
from app.models import AgentTask, User
from app.schemas import TaskCreateIn, TaskOut
from app.services import actions as action_service
from app.services import tasks as task_service

router = APIRouter(tags=["tasks"])


def get_task_runner(settings: Settings = Depends(get_settings)) -> task_service.TaskRunner:
    """Background runner with its own sessions (the request's session is closed by then)."""
    return task_service.TaskRunner(get_sessionmaker(), settings)


def task_view(task: AgentTask, settings: Settings, *, with_steps: bool = True) -> dict[str, Any]:
    stale_after = settings.task_lease_seconds
    out: dict[str, Any] = {
        "id": task.id,
        "kind": task.kind,
        "goal": task.goal,
        "status": task.status,
        "progress": task.progress,
        "current_step": task.current_step,
        "total_steps": task.total_steps,
        "input": task.input,
        "result": task.result,
        "error": task.error,
        "cancel_requested": task.cancel_requested,
        "attempt": task.attempt,
        "interrupted": task_service.is_interrupted(task),
        "stalled": task_service.is_stalled(task, stale_after_seconds=stale_after),
        "resumable": task_service.is_resumable(task, stale_after_seconds=stale_after) and not task.cancel_requested,
        "state": task.state,
        "proposals": [action_service.action_view(a) for a in task.proposals],
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }
    if with_steps:
        out["steps"] = [
            {
                "position": s.position,
                "name": s.name,
                "tool": s.tool,
                "status": s.status,
                "attempts": s.attempts,
                "max_attempts": s.max_attempts,
                "args": s.args,
                "output": s.output,
                "error": s.error,
                "decision": s.decision,
                "started_at": s.started_at,
                "finished_at": s.finished_at,
            }
            for s in task.steps
        ]
    return out


@router.post("/tasks", response_model=TaskOut, status_code=201, dependencies=[Depends(write_limit)])
def create_task(
    body: TaskCreateIn,
    response: Response,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    runner: task_service.TaskRunner = Depends(get_task_runner),
) -> dict[str, Any]:
    task, created = task_service.create_task(db, user.id, body.kind, body.input, body.idempotency_key, settings)
    if created:
        background.add_task(runner.run, task.id)  # runs after the response is sent
    else:
        response.status_code = 200  # same idempotency key: the original task, not a new one
    return task_view(task, settings)


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    user: User = Depends(get_current_user), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> list[dict[str, Any]]:
    return [task_view(t, settings, with_steps=False) for t in task_service.list_tasks(db, user.id)]


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return task_view(task_service.get_owned(db, user.id, task_id), settings)


@router.post("/tasks/{task_id}/cancel", response_model=TaskOut, dependencies=[Depends(write_limit)])
def cancel_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return task_view(task_service.request_cancel(db, user.id, task_id), settings)


@router.post("/tasks/{task_id}/resume", response_model=TaskOut, dependencies=[Depends(write_limit)])
def resume_task(
    task_id: str,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    runner: task_service.TaskRunner = Depends(get_task_runner),
) -> dict[str, Any]:
    task = task_service.prepare_resume(db, user.id, task_id, settings)
    background.add_task(runner.run, task.id)
    return task_view(task, settings)
