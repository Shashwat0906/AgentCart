from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.providers import MockProvider, build_provider
from app.providers.base import ProviderError, parse_json_object
from app.providers.remote import AnthropicProvider, OllamaProvider, OpenAICompatibleProvider


def test_parse_json_object_variants() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('Sure!\n```json\n{"a": 2}\n```') == {"a": 2}
    with pytest.raises(ValueError):
        parse_json_object("no json here")
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


def test_factory_falls_back_to_mock_without_keys() -> None:
    assert isinstance(build_provider(Settings(llm_provider="openai_compatible", hosted_llm_api_key="")), MockProvider)
    assert isinstance(build_provider(Settings(llm_provider="anthropic", anthropic_api_key="")), MockProvider)
    assert isinstance(build_provider(Settings(llm_provider="ollama")), OllamaProvider)


@pytest.fixture()
def fake_http(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Route provider HTTP calls to a scripted transport."""
    calls: list[dict[str, Any]] = []
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "headers": dict(request.headers), "body": request.content.decode()})
        return responses.pop(0) if responses else httpx.Response(500)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls.append({"responses": responses})  # type: ignore[dict-item]
    return calls


def test_openai_compatible_success(fake_http: list[dict[str, Any]]) -> None:
    fake_http[0]["responses"].append(
        httpx.Response(200, json={"choices": [{"message": {"content": '{"category": "laptops"}'}}]})
    )
    p = OpenAICompatibleProvider("https://llm.example/v1", "k-123", "m", timeout=5, max_retries=0)
    assert p.complete_json("sys", "user") == {"category": "laptops"}
    call = fake_http[1]
    assert call["url"] == "https://llm.example/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer k-123"
    assert '"response_format"' in call["body"]


def test_retries_are_bounded(fake_http: list[dict[str, Any]]) -> None:
    p = OllamaProvider("http://ollama.local", "m", timeout=5, max_retries=2)
    with pytest.raises(ProviderError):
        p.complete_text("sys", "user")
    assert len(fake_http) - 1 == 3  # 1 attempt + 2 retries, then give up


def test_retry_then_success(fake_http: list[dict[str, Any]]) -> None:
    fake_http[0]["responses"].extend([httpx.Response(503), httpx.Response(200, json={"message": {"content": "hello"}})])
    p = OllamaProvider("http://ollama.local", "m", timeout=5, max_retries=2)
    assert p.complete_text("sys", "user") == "hello"


def test_anthropic_request_shape(fake_http: list[dict[str, Any]]) -> None:
    fake_http[0]["responses"].append(httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]}))
    p = AnthropicProvider("sk-test", "claude-haiku-5-5", timeout=5, max_retries=0)
    assert p.complete_text("sys", "user") == "ok"
    call = fake_http[1]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-test" and call["headers"]["anthropic-version"] == "2023-06-01"


def test_deadline_stops_provider_before_calling(fake_http: list[dict[str, Any]]) -> None:
    p = OllamaProvider("http://ollama.local", "m", timeout=5, max_retries=3)
    with pytest.raises(ProviderError, match="deadline"):
        p.complete_text("sys", "user", deadline=time.monotonic() - 1)
    assert len(fake_http) == 1  # no HTTP request was made
