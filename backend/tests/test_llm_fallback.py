"""A configured hosted LLM (e.g. Google Gemini's free tier) that fails must never break the app: chat falls back to
the rule-based parser and summary, tasks stay rule-based, and nothing switches provider on its own."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_provider
from app.main import app
from app.providers.remote import OpenAICompatibleProvider
from tests.conftest import register

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
FAKE_KEY = "test-key-not-real-0123456789"
QUERY = "a wireless mouse under ₹3,000"


@pytest.fixture()
def gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[Callable[[httpx.Request], httpx.Response]], list[str]]]:
    """Serve a Gemini-configured provider whose HTTP replies come from `handler`; returns the requested URLs."""
    real_client = httpx.Client
    monkeypatch.setattr(time, "sleep", lambda s: None)
    previous = app.dependency_overrides.get(get_provider)

    def use(handler: Callable[[httpx.Request], httpx.Response]) -> list[str]:
        urls: list[str] = []

        def recording(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return handler(request)

        monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(recording), **kw))
        provider = OpenAICompatibleProvider(
            GEMINI_URL,
            FAKE_KEY,
            "gemini-2.5-flash-lite",
            timeout=5,
            max_retries=1,
            reasoning_effort="none",
            json_mode=False,
        )
        app.dependency_overrides[get_provider] = lambda: provider
        return urls

    yield use
    if previous is not None:
        app.dependency_overrides[get_provider] = previous


def recommended(body: dict[str, Any]) -> list[int]:
    payload = body["assistant_message"]["payload"]
    return [int(r["product_id"]) for r in payload.get("recommendations") or []]


def chat(client: TestClient, h: dict[str, str]) -> dict[str, Any]:
    r = client.post("/api/chat", headers=h, json={"message": QUERY})
    assert r.status_code == 200, r.text
    return r.json()


def timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("read timed out", request=request)


@pytest.mark.parametrize(
    ("handler", "max_calls"),
    [
        (lambda r: httpx.Response(400, json={"error": {"message": "Invalid value for reasoning_effort"}}), 2),
        (lambda r: httpx.Response(401, json={"error": {"message": "API key not valid"}}), 2),  # invalid key
        (lambda r: httpx.Response(403, json={}), 2),  # no access
        (lambda r: httpx.Response(429, json={}), 2),  # free-tier quota or rate limit reached
        (lambda r: httpx.Response(503), 4),  # outage: retried within bounds
        (timeout, 4),  # slow provider: retried within bounds
        (lambda r: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}), 4),  # empty answer
    ],
    ids=["bad-request-400", "invalid-key-401", "no-access-403", "quota-429", "outage-503", "timeout", "empty-answer"],
)
def test_chat_falls_back_to_the_rules_when_the_hosted_llm_fails(
    client: TestClient, gemini: Any, handler: Callable[[httpx.Request], httpx.Response], max_calls: int
) -> None:
    h = register(client)
    baseline = recommended(chat(client, h))  # conftest's mock provider: rules only
    urls = gemini(handler)
    body = chat(client, h)
    assert recommended(body) == baseline and baseline  # same products, from the rules
    assert urls and all(u == GEMINI_URL + "/chat/completions" for u in urls)
    assert len(urls) <= max_calls  # at most two model calls per message, each within its retry budget
    assert FAKE_KEY not in str(body)


def test_a_working_hosted_llm_is_used_and_cannot_change_the_products(client: TestClient, gemini: Any) -> None:
    h = register(client)
    baseline = recommended(chat(client, h))

    def ok(request: httpx.Request) -> httpx.Response:
        wants_json = "JSON object" in request.content.decode()
        content = '{"category": "laptops", "budget_max": 999999}' if wants_json else "Here are good wireless mice."
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    urls = gemini(ok)
    body = chat(client, h)
    assert urls  # the hosted model was called
    assert recommended(body) == baseline  # the parser's category and budget win: the model can only fill gaps


def test_health_reports_the_configured_provider_and_mock_by_default(client: TestClient, gemini: Any) -> None:
    assert client.get("/api/health/ready").json()["llm_provider"] == "mock"
    gemini(lambda r: httpx.Response(200, json={}))
    ready = client.get("/api/health/ready")
    assert ready.json()["llm_provider"] == "openai_compatible" and FAKE_KEY not in ready.text
    assert client.get("/api/config/public").json()["llm_provider"] == "openai_compatible"


def test_agent_tasks_stay_rule_based_with_a_hosted_llm(client: TestClient, gemini: Any) -> None:
    from tests.test_tasks import fetch

    urls = gemini(lambda r: httpx.Response(500))
    h = register(client)
    t = client.post("/api/tasks", headers=h, json={"kind": "shopping_agent", "input": {"goal": QUERY}}).json()
    t = fetch(client, h, t["id"])
    assert t["status"] == "succeeded" and t["state"]["planner"] == "rule_based" and urls == []  # TASK_PLANNER default
