"""Task controls (Phase 2.1): fresh vs stalled queued tasks, resume eligibility, and "Run again"."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import app.api.routes_tasks as routes_tasks
from app.core.config import get_settings
from app.main import app
from app.models import AgentTask
from app.services import tasks as task_service
from tests.conftest import register

INPUT = {"category": "laptops", "max_price_inr": 50_000}


class _NoRun:
    """Stands in for the background runner so a task's state stays exactly as the test set it."""

    def run(self, task_id: str) -> None:
        return None


@pytest.fixture()
def no_background_runs(client: TestClient) -> None:
    app.dependency_overrides[routes_tasks.get_task_runner] = lambda: _NoRun()


def uid(client: TestClient, h: dict[str, str]) -> int:
    return int(client.get("/api/auth/me", headers=h).json()["id"])


def make(SessionLocal: sessionmaker[Session], user_id: int, state: str) -> str:
    """Create a task directly (no run) and put it into `state`."""
    with SessionLocal() as s:
        task, _ = task_service.create_task(s, user_id, "product_research", INPUT, None, get_settings())
        row = s.get(AgentTask, task.id)
        assert row is not None
        now = datetime.now(UTC)
        lease = timedelta(seconds=get_settings().task_lease_seconds)
        if state == "queued_stalled":
            row.updated_at = now - lease - timedelta(seconds=1)
        elif state == "running_live":
            row.status, row.locked_by, row.locked_until = "running", "r1", now + lease
        elif state == "running_interrupted":
            row.status, row.locked_by, row.locked_until = "running", "dead", now - timedelta(seconds=1)
        elif state == "succeeded":
            row.status, row.progress, row.finished_at = "succeeded", 100, now
        elif state == "failed_retryable":
            row.status, row.finished_at = "failed", now
            row.error = {"code": "temporarily_unavailable", "message": "x", "retryable": True}
        elif state == "failed_permanent":
            row.status, row.finished_at = "failed", now
            row.error = {"code": "not_found", "message": "x", "retryable": False}
        elif state == "cancelled":
            row.status, row.finished_at = "cancelled", now
        else:
            assert state == "queued_fresh"
        s.commit()
        return task.id


def get(client: TestClient, h: dict[str, str], tid: str) -> dict[str, Any]:
    r = client.get(f"/api/tasks/{tid}", headers=h)
    assert r.status_code == 200
    return r.json()


def task_count(SessionLocal: sessionmaker[Session], user_id: int) -> int:
    with SessionLocal() as s:
        return int(s.scalar(select(func.count()).select_from(AgentTask).where(AgentTask.user_id == user_id)) or 0)


# ------------------------------------------------------------------ fresh vs stalled queued tasks
def test_a_freshly_queued_task_is_not_resumable(
    client: TestClient, SessionLocal: sessionmaker[Session], no_background_runs: None
) -> None:
    h = register(client)
    tid = make(SessionLocal, uid(client, h), "queued_fresh")
    t = get(client, h, tid)
    assert (t["status"], t["stalled"], t["resumable"], t["interrupted"]) == ("queued", False, False, False)
    r = client.post(f"/api/tasks/{tid}/resume", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"] == {"status": "queued", "reason": "queued"}
    assert client.post(f"/api/tasks/{tid}/cancel", headers=h).status_code == 200  # still cancellable


def test_a_stalled_queued_task_is_resumable_and_completes(
    client: TestClient, SessionLocal: sessionmaker[Session]
) -> None:
    h = register(client)
    tid = make(SessionLocal, uid(client, h), "queued_stalled")
    t = get(client, h, tid)
    assert (t["status"], t["stalled"], t["resumable"]) == ("queued", True, True)
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 200
    done = get(client, h, tid)  # the resumed background run has finished by now
    assert done["status"] == "succeeded" and done["stalled"] is False and done["resumable"] is False


def test_resuming_a_stalled_task_restarts_its_grace_period(
    client: TestClient, SessionLocal: sessionmaker[Session], no_background_runs: None
) -> None:
    h = register(client)
    tid = make(SessionLocal, uid(client, h), "queued_stalled")
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 200
    t = get(client, h, tid)
    assert (t["status"], t["stalled"], t["resumable"]) == ("queued", False, False)
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 409  # a double click schedules nothing


def test_stall_threshold_is_the_runner_lease(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    tid = make(SessionLocal, uid(client, h), "queued_fresh")
    lease = get_settings().task_lease_seconds
    with SessionLocal() as s:
        row = s.get(AgentTask, tid)
        assert row is not None
        changed = row.updated_at.replace(tzinfo=UTC) if row.updated_at.tzinfo is None else row.updated_at
        just_before = changed + timedelta(seconds=lease - 1)
        at_threshold = changed + timedelta(seconds=lease)
        assert not task_service.is_stalled(row, stale_after_seconds=lease, now=just_before)
        assert task_service.is_stalled(row, stale_after_seconds=lease, now=at_threshold)
        assert not task_service.is_resumable(row, stale_after_seconds=lease, now=just_before)
        assert task_service.is_resumable(row, stale_after_seconds=lease, now=at_threshold)


# ------------------------------------------------------------------ resume eligibility for every state
@pytest.mark.parametrize(
    ("state", "resumable", "resume_code", "cancel_code"),
    [
        ("queued_fresh", False, 409, 200),
        ("queued_stalled", True, 200, 200),
        ("running_live", False, 409, 200),
        ("running_interrupted", True, 200, 200),
        ("succeeded", False, 409, 409),
        ("failed_retryable", True, 200, 409),
        ("failed_permanent", False, 409, 409),
        ("cancelled", False, 409, 409),
    ],
)
def test_resume_and_cancel_eligibility_by_state(
    client: TestClient,
    SessionLocal: sessionmaker[Session],
    no_background_runs: None,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    resumable: bool,
    resume_code: int,
    cancel_code: int,
) -> None:
    monkeypatch.setattr(get_settings(), "task_max_active_per_user", 50)
    h = register(client)
    user_id = uid(client, h)
    assert get(client, h, make(SessionLocal, user_id, state))["resumable"] is resumable
    assert client.post(f"/api/tasks/{make(SessionLocal, user_id, state)}/resume", headers=h).status_code == resume_code
    assert client.post(f"/api/tasks/{make(SessionLocal, user_id, state)}/cancel", headers=h).status_code == cancel_code


def test_a_cancel_request_blocks_resume_even_when_stalled(
    client: TestClient, SessionLocal: sessionmaker[Session], no_background_runs: None
) -> None:
    h = register(client)
    tid = make(SessionLocal, uid(client, h), "running_interrupted")
    with SessionLocal() as s:
        row = s.get(AgentTask, tid)
        assert row is not None
        row.cancel_requested = True
        s.commit()
    assert get(client, h, tid)["resumable"] is False
    assert client.post(f"/api/tasks/{tid}/resume", headers=h).status_code == 409


def test_resume_still_requires_ownership(
    client: TestClient, SessionLocal: sessionmaker[Session], no_background_runs: None
) -> None:
    alice, bob = register(client), register(client)
    tid = make(SessionLocal, uid(client, alice), "queued_stalled")
    assert client.post(f"/api/tasks/{tid}/resume", headers=bob).status_code == 404
    assert get(client, alice, tid)["status"] == "queued"


# ------------------------------------------------------------------ "Run again" end to end
def test_stored_input_round_trips_through_create(client: TestClient) -> None:
    """Run again re-submits a task's stored input; the server must accept it exactly as it returned it."""
    h = register(client)
    first = client.post("/api/tasks", json={"kind": "product_research", "input": INPUT}, headers=h).json()
    echoed = get(client, h, first["id"])["input"]
    assert set(echoed) >= {"category", "max_price_inr", "in_stock", "compare_top", "include_cart"}
    again = client.post(
        "/api/tasks",
        json={"kind": "product_research", "input": echoed, "idempotency_key": "rerun-roundtrip-1"},
        headers=h,
    )
    assert again.status_code == 201 and again.json()["input"] == echoed


@pytest.mark.parametrize("state", ["succeeded", "cancelled", "failed_permanent", "failed_retryable"])
def test_run_again_creates_a_new_task_and_leaves_the_original_untouched(
    client: TestClient, SessionLocal: sessionmaker[Session], state: str
) -> None:
    h = register(client)
    user_id = uid(client, h)
    original_id = make(SessionLocal, user_id, state)
    original = get(client, h, original_id)
    body = {
        "kind": "product_research",
        "input": {"category": "laptops", "max_price_inr": 50_000},
        "idempotency_key": f"rerun-{original_id}-1",
    }

    first = client.post("/api/tasks", json=body, headers=h)
    assert first.status_code == 201 and first.json()["id"] != original_id
    run = get(client, h, first.json()["id"])
    assert run["status"] == "succeeded" and run["result"]["all_within_budget"] is True
    assert all(p["price_inr"] <= 50_000 for p in run["result"]["top"])

    # A duplicate click (same key) returns the same new task instead of creating another one.
    dup = client.post("/api/tasks", json=body, headers=h)
    assert dup.status_code == 200 and dup.json()["id"] == first.json()["id"]
    assert task_count(SessionLocal, user_id) == 2

    after = get(client, h, original_id)
    assert {k: after[k] for k in ("status", "result", "error", "finished_at")} == {
        k: original[k] for k in ("status", "result", "error", "finished_at")
    }


def test_run_again_keys_are_per_attempt(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    body = {"kind": "product_research", "input": INPUT}
    a = client.post("/api/tasks", json={**body, "idempotency_key": "rerun-attempt-1"}, headers=h).json()["id"]
    b = client.post("/api/tasks", json={**body, "idempotency_key": "rerun-attempt-2"}, headers=h).json()["id"]
    assert a != b and task_count(SessionLocal, uid(client, h)) == 2


def test_run_again_cannot_smuggle_extra_fields(client: TestClient, SessionLocal: sessionmaker[Session]) -> None:
    h = register(client)
    extras: list[dict[str, Any]] = [{"status": "succeeded"}, {"steps": []}, {"user_id": 1}, {"id": "x"}]
    for extra in extras:
        r = client.post("/api/tasks", json={"kind": "product_research", "input": INPUT, **extra}, headers=h)
        assert r.status_code == 422, extra
    assert task_count(SessionLocal, uid(client, h)) == 0


def test_run_again_respects_the_active_task_limit(
    client: TestClient, SessionLocal: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "task_max_active_per_user", 1)
    h = register(client)
    make(SessionLocal, uid(client, h), "queued_fresh")
    r = client.post(
        "/api/tasks", json={"kind": "product_research", "input": INPUT, "idempotency_key": "rerun-limit-01"}, headers=h
    )
    assert r.status_code == 429
