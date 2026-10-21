"""/api/health/ready reports the deployed commit (Render's RENDER_GIT_COMMIT) and nothing else from the environment."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app

SHA = "115a2994f0c1b0e7d0c1a6c6b8e4c3d2a1f0e9d8"
READY_KEYS = {"status", "database", "products", "llm_provider", "commit"}


def ready(client: TestClient) -> tuple[int, dict[str, Any], str]:
    r = client.get("/api/health/ready")
    return r.status_code, r.json(), r.text


def test_the_deployed_commit_is_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    status, body, _ = ready(client)
    assert status == 200 and set(body) == READY_KEYS
    assert body["commit"] == SHA and body["status"] == "ready" and body["products"] == 115


def test_a_short_hash_is_reported_too(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", "115a299")
    assert ready(client)[1]["commit"] == "115a299"


def test_no_commit_outside_render(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    status, body, _ = ready(client)
    assert status == 200 and body["commit"] is None and set(body) == READY_KEYS


@pytest.mark.parametrize(
    "value",
    [
        "",
        "main",  # a branch name, not a hash
        "115A2994F0C1",  # uppercase is not what git/Render produce
        "abc123",  # too short
        SHA + "0",  # 41 characters
        f"{SHA}; cat /etc/passwd",
        f"{SHA}\nJWT_SECRET=leak",
        "postgresql://user:pass@host/db",
    ],
)
def test_anything_but_a_commit_hash_is_never_echoed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", value)
    _, body, text = ready(client)
    assert body["commit"] is None
    assert not value or value not in text


def test_the_response_carries_no_other_environment_value(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    monkeypatch.setenv("JWT_SECRET", "s3cr3t-value-that-must-not-leak-" + "x" * 16)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-must-not-leak-0123456789")
    _, body, text = ready(client)
    leaks = [k for k, v in os.environ.items() if len(v) >= 12 and v != SHA and v in text]
    assert leaks == [] and body["commit"] == SHA
    assert json.dumps(body, sort_keys=True) == json.dumps(
        {"commit": SHA, "database": "up", "llm_provider": body["llm_provider"], "products": 115, "status": "ready"},
        sort_keys=True,
    )


def test_an_unavailable_database_still_reports_only_the_commit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Down:
        def execute(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("connection refused to db-host:5432 password=hunter2")

    def broken_db() -> Iterator[Down]:
        yield Down()

    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = broken_db
    try:
        status, body, text = ready(client)
    finally:
        if previous is not None:
            app.dependency_overrides[get_db] = previous
    assert status == 503 and body == {"status": "unavailable", "database": "down", "commit": SHA}
    assert "hunter2" not in text and "db-host" not in text
