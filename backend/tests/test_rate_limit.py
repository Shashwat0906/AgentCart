"""Rate limiting: allowance, rejection, recovery, Retry-After, and client-IP resolution behind proxies."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core import rate_limit
from app.core.config import get_settings
from app.core.rate_limit import SlidingWindowLimiter, client_ip

BAD_LOGIN = {"email": "nobody@example.com", "password": "WrongPass123"}


@pytest.fixture()
def limited(monkeypatch: pytest.MonkeyPatch) -> int:
    """Rate limiting on with the production default for auth (10/min) and one trusted proxy."""
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_auth_per_minute", 10)
    monkeypatch.setattr(s, "trusted_proxy_hops", 1)
    return 10


def _req(xff: list[str] | None = None, peer: str = "10.0.0.1") -> Request:
    headers = [(b"x-forwarded-for", v.encode()) for v in (xff or [])]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234)})


def _login(client: TestClient, xff: str | None = None) -> int:
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    return client.post("/api/auth/login", json=BAD_LOGIN, headers=headers).status_code


# ------------------------------------------------------------------ limiter unit behaviour
def test_limiter_allows_up_to_limit_then_blocks_and_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    lim = SlidingWindowLimiter()
    assert [lim.hit("k", 3) for _ in range(3)] == [None, None, None]
    assert lim.hit("k", 3) == pytest.approx(60.0)
    now[0] += 30
    assert lim.hit("k", 3) == pytest.approx(30.0)  # rejected hits don't extend the window
    now[0] += 30
    assert lim.hit("k", 3) is None  # window has slid past the first hits
    assert lim.hit("other", 3) is None  # keys are independent


# ------------------------------------------------------------------ client IP resolution
@pytest.mark.parametrize(
    ("xff", "hops", "expected"),
    [
        (None, 1, "10.0.0.1"),  # direct connection: socket peer
        (["198.51.100.7"], 1, "198.51.100.7"),  # one proxy appended the real client
        (["6.6.6.6, 198.51.100.7"], 1, "198.51.100.7"),  # client-forged prefix ignored
        (["6.6.6.6", "198.51.100.7"], 1, "198.51.100.7"),  # repeated headers are one list
        (["6.6.6.6, 198.51.100.7, 172.16.0.9"], 2, "198.51.100.7"),  # two proxies
        (["198.51.100.7"], 2, "198.51.100.7"),  # fewer entries than hops: left-most proxy record
        (["6.6.6.6, 198.51.100.7"], 0, "10.0.0.1"),  # hops=0 ignores the header entirely
        (["6.6.6.6, 2001:DB8::1"], 1, "2001:db8::1"),  # IPv6 normalised
        ([" , 198.51.100.7 , "], 1, "198.51.100.7"),  # empty entries skipped
        (["6.6.6.6, not-an-ip"], 1, "not-an-ip"),  # malformed proxy entry is still a stable key
        # Ports some proxies append are the client's per-connection source port: never part of the key.
        (["6.6.6.6, 198.51.100.7:51234"], 1, "198.51.100.7"),
        (["6.6.6.6, [2001:db8::1]:443"], 1, "2001:db8::1"),
        (["[2001:db8::1]"], 1, "2001:db8::1"),  # bracketed without port
        (["2001:db8::1:443"], 1, "2001:db8::1:443"),  # bare IPv6: last group is not a port
        (["198.51.100.7:http"], 1, "198.51.100.7:http"),  # non-numeric "port": left as a stable key
    ],
)
def test_client_ip_uses_the_proxy_written_entry(
    monkeypatch: pytest.MonkeyPatch, xff: list[str] | None, hops: int, expected: str
) -> None:
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", hops)
    assert client_ip(_req(xff)) == expected


# ------------------------------------------------------------------ API behaviour
def test_normal_auth_traffic_is_not_limited(client: TestClient, limited: int) -> None:
    # Register, then log in successfully, staying under the limit: every request succeeds.
    email, pw = "normal@example.com", "Passw0rd!x"
    assert (
        client.post("/api/auth/register", json={"email": email, "password": pw, "display_name": "N"}).status_code == 201
    )
    for _ in range(limited - 1):
        assert client.post("/api/auth/login", json={"email": email, "password": pw}).status_code == 200


def test_excessive_auth_traffic_gets_429_with_retry_after(client: TestClient, limited: int) -> None:
    codes = [_login(client) for _ in range(limited + 2)]
    assert codes == [401] * limited + [429, 429]
    r = client.post("/api/auth/login", json=BAD_LOGIN)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert r.json()["error"]["details"]["retry_after_seconds"] <= int(r.headers["Retry-After"])


def test_register_and_login_share_the_auth_budget(client: TestClient, limited: int) -> None:
    for i in range(limited):
        r = client.post(
            "/api/auth/register", json={"email": f"u{i}@example.com", "password": "Passw0rd!x", "display_name": "U"}
        )
        assert r.status_code == 201
    assert _login(client) == 429


def test_spoofed_forwarded_for_cannot_bypass_the_limit(client: TestClient, limited: int) -> None:
    # Same real client (proxy-appended right-most entry), a different forged prefix every time.
    codes = [_login(client, f"203.0.113.{i}, 198.51.100.7") for i in range(limited + 5)]
    assert codes == [401] * limited + [429] * 5


def test_spoofing_blocked_under_uvicorn_trust_all_proxy_headers(client: TestClient, limited: int) -> None:
    # Production runs uvicorn with --proxy-headers --forwarded-allow-ips="*" (render.yaml, compose),
    # which rewrites request.client to the *left-most*, client-controlled X-Forwarded-For entry.
    # Reproduce that stack in-process: the limit must still hold.
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    with TestClient(ProxyHeadersMiddleware(client.app, trusted_hosts="*")) as proxied:  # type: ignore[arg-type]
        codes = [_login(proxied, f"203.0.113.{i}, 198.51.100.7") for i in range(limited + 5)]
    assert codes == [401] * limited + [429] * 5


def test_rotating_source_ports_cannot_bypass_the_limit(client: TestClient, limited: int) -> None:
    # A proxy that records "ip:port" sees a new source port on every new connection; with forged
    # prefixes and rotating ports the client must still have one budget.
    codes = [_login(client, f"203.0.113.{i}, 198.51.100.7:{40000 + i}") for i in range(limited + 3)]
    assert codes == [401] * limited + [429] * 3
    codes6 = [_login(client, f"[2001:db8::7]:{50000 + i}") for i in range(limited + 1)]
    assert codes6[-1] == 429


def test_distinct_clients_behind_the_proxy_have_separate_budgets(client: TestClient, limited: int) -> None:
    assert [_login(client, "198.51.100.7") for _ in range(limited + 1)][-1] == 429
    # Another real client is unaffected by the first one's exhaustion.
    assert _login(client, "198.51.100.8") == 401


def test_rate_limiting_disabled_never_returns_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    assert 429 not in {_login(client) for _ in range(25)}


def test_per_user_chat_limit_also_sends_retry_after(
    client: TestClient, auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_enabled", True)
    monkeypatch.setattr(s, "rate_limit_chat_per_minute", 2)
    codes = [client.post("/api/chat", json={"message": "hi"}, headers=auth).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = client.post("/api/chat", json={"message": "hi"}, headers=auth)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
