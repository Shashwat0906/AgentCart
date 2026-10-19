"""CORS: browser preflights from the deployed frontend succeed; other origins get no CORS grant."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app

FRONTEND = "https://agent-cart-sigma.vercel.app"
PREFLIGHT_REGISTER = {
    "Origin": FRONTEND,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type",
}


@pytest.fixture()
def make_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Build an app with a given CORS_ORIGINS value (CORS is configured when the app is created).

    Reuses the `client` fixture's test-database overrides.
    """
    opened: list[TestClient] = []

    def _make(cors_origins: str) -> TestClient:
        monkeypatch.setenv("CORS_ORIGINS", cors_origins)
        get_settings.cache_clear()
        fresh: FastAPI = create_app()
        fresh.dependency_overrides = dict(client.app.dependency_overrides)  # type: ignore[attr-defined]
        c = TestClient(fresh)
        opened.append(c)
        return c

    yield _make
    for c in opened:
        c.close()
    get_settings.cache_clear()


def test_register_preflight_from_production_frontend_succeeds(make_client) -> None:  # type: ignore[no-untyped-def]
    c = make_client(FRONTEND)
    r = c.options("/api/auth/register", headers=PREFLIGHT_REGISTER)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == FRONTEND
    assert "POST" in r.headers["access-control-allow-methods"]
    assert "content-type" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in r.headers  # bearer tokens, no cookies
    assert "Origin" in r.headers["vary"]


def test_preflight_is_answered_before_auth_and_validation(make_client) -> None:  # type: ignore[no-untyped-def]
    c = make_client(FRONTEND)
    # Authenticated route: preflight carries no token and must not get 401.
    r = c.options(
        "/api/cart/items",
        headers={
            "Origin": FRONTEND,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == FRONTEND
    assert {"authorization", "content-type"} <= {
        h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")
    }
    # Register with no body: preflight must not reach request validation (422) or method routing (405).
    assert c.options("/api/auth/register", headers=PREFLIGHT_REGISTER).status_code == 200


def test_actual_register_request_carries_allow_origin(make_client) -> None:  # type: ignore[no-untyped-def]
    c = make_client(FRONTEND)
    r = c.post(
        "/api/auth/register",
        json={"email": "cors@example.com", "password": "Passw0rd!x", "display_name": "Cors"},
        headers={"Origin": FRONTEND},
    )
    assert r.status_code == 201, r.text
    assert r.headers["access-control-allow-origin"] == FRONTEND


@pytest.mark.parametrize(
    "origin",
    [
        "https://agentcart.vercel.app",  # the placeholder that caused the production failure
        "https://evil.example",
        FRONTEND + ".evil.example",  # suffix trick
        "http://agent-cart-sigma.vercel.app",  # wrong scheme
        "null",
    ],
)
def test_other_origins_get_no_cors_grant(make_client, origin: str) -> None:  # type: ignore[no-untyped-def]
    c = make_client(FRONTEND)
    pre = c.options("/api/auth/register", headers={**PREFLIGHT_REGISTER, "Origin": origin})
    assert pre.status_code == 400
    assert "access-control-allow-origin" not in pre.headers
    simple = c.get("/api/health", headers={"Origin": origin})
    assert simple.status_code == 200 and "access-control-allow-origin" not in simple.headers


@pytest.mark.parametrize(
    "configured",
    [
        FRONTEND,
        FRONTEND + "/",  # pasted from the address bar
        f" {FRONTEND}/ ",
        f'"{FRONTEND}"',
        f"http://localhost:5173, {FRONTEND}/",  # list with other origins
    ],
)
def test_configured_origin_is_normalised(make_client, configured: str) -> None:  # type: ignore[no-untyped-def]
    c = make_client(configured)
    r = c.options("/api/auth/register", headers=PREFLIGHT_REGISTER)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == FRONTEND


def test_never_wildcard(make_client) -> None:  # type: ignore[no-untyped-def]
    c = make_client(f"http://localhost:5173,{FRONTEND}")
    for origin in (FRONTEND, "http://localhost:5173"):
        r = c.options("/api/auth/register", headers={**PREFLIGHT_REGISTER, "Origin": origin})
        assert r.headers["access-control-allow-origin"] == origin != "*"


def test_allowed_origins_are_logged_at_startup(make_client, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level("INFO", logger="agentcart"):
        make_client(FRONTEND + "/")
    assert f"CORS allowed origins: {FRONTEND}" in caplog.text
