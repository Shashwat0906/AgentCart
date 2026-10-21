from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.providers import MockProvider, build_provider
from app.providers.base import ProviderError, ProviderRejected, parse_json_object
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


# ------------------------------------------------------------------ Google Gemini via the OpenAI-compatible endpoint
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
FAKE_KEY = "test-key-not-real-0123456789"  # never a real key


def _ok(content: str | None = '{"category": "laptops"}') -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.parametrize("base", [GEMINI_URL, GEMINI_URL + "/"])
def test_gemini_request_shape(fake_http: list[dict[str, Any]], base: str) -> None:
    fake_http[0]["responses"].append(_ok())
    p = OpenAICompatibleProvider(
        base, FAKE_KEY, "gemini-2.5-flash-lite", timeout=5, max_retries=0, reasoning_effort="none", json_mode=False
    )
    assert p.complete_json("sys", "user") == {"category": "laptops"}
    call = fake_http[1]
    body = json.loads(call["body"])
    assert call["url"] == GEMINI_URL + "/chat/completions"
    assert call["headers"]["authorization"] == f"Bearer {FAKE_KEY}"
    assert body["model"] == "gemini-2.5-flash-lite" and body["reasoning_effort"] == "none"
    assert "response_format" not in body  # JSON is asked for in the prompt instead
    assert "single JSON object" in body["messages"][0]["content"] and body["messages"][1] == {
        "role": "user",
        "content": "user",
    }
    assert body["temperature"] == 0 and body["max_tokens"] == 600


def test_text_requests_are_not_changed_by_json_settings(fake_http: list[dict[str, Any]]) -> None:
    fake_http[0]["responses"].append(_ok("A short polished summary."))
    p = OpenAICompatibleProvider(
        GEMINI_URL, FAKE_KEY, "m", timeout=5, max_retries=0, reasoning_effort="none", json_mode=False
    )
    assert p.complete_text("sys", "user") == "A short polished summary."
    body = json.loads(fake_http[1]["body"])
    assert (
        body["messages"][0]["content"] == "sys" and "response_format" not in body and body["reasoning_effort"] == "none"
    )


def test_defaults_are_unchanged_for_other_openai_compatible_endpoints(fake_http: list[dict[str, Any]]) -> None:
    fake_http[0]["responses"].append(_ok())
    OpenAICompatibleProvider("https://llm.example/v1", FAKE_KEY, "m", timeout=5, max_retries=0).complete_json("s", "u")
    body = json.loads(fake_http[1]["body"])
    assert body["response_format"] == {"type": "json_object"} and "reasoning_effort" not in body


@pytest.mark.parametrize("content", ["", "   ", None])
def test_an_empty_answer_is_a_provider_error(fake_http: list[dict[str, Any]], content: str | None) -> None:
    fake_http[0]["responses"].append(_ok(content))
    p = OpenAICompatibleProvider(GEMINI_URL, FAKE_KEY, "m", timeout=5, max_retries=0)
    with pytest.raises(ProviderError):
        p.complete_text("sys", "user")


# ------------------------------------------------------------------ configuration
def gemini_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_provider": "openai_compatible",
        "hosted_llm_base_url": GEMINI_URL,
        "hosted_llm_api_key": FAKE_KEY,
        "hosted_llm_model": "gemini-2.5-flash-lite",
        "hosted_llm_reasoning_effort": "none",
        "hosted_llm_json_mode": False,
    }
    return Settings(**{**values, **overrides})


def test_factory_builds_the_gemini_provider_from_settings() -> None:
    p = build_provider(gemini_settings(llm_timeout_seconds=7, llm_max_retries=1))
    assert isinstance(p, OpenAICompatibleProvider) and p.is_llm and p.name == "openai_compatible"
    assert (p.base_url, p.model, p.reasoning_effort, p.json_mode) == (
        GEMINI_URL,
        "gemini-2.5-flash-lite",
        "none",
        False,
    )
    assert (p.timeout, p.max_retries) == (7, 1)


def test_mock_is_the_default_and_the_fallback_without_a_key() -> None:
    assert Settings.model_fields["llm_provider"].default == "mock"
    assert Settings.model_fields["hosted_llm_api_key"].default == ""
    assert isinstance(build_provider(Settings(llm_provider="mock")), MockProvider)
    fallback = build_provider(gemini_settings(hosted_llm_api_key=""))  # Gemini configured, key missing
    assert isinstance(fallback, MockProvider) and fallback.is_llm is False


def test_settings_read_the_environment_variable_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "LLM_PROVIDER": "openai_compatible",
        "HOSTED_LLM_BASE_URL": GEMINI_URL,
        "HOSTED_LLM_API_KEY": FAKE_KEY,
        "HOSTED_LLM_MODEL": "gemini-2.5-flash-lite",
        "HOSTED_LLM_REASONING_EFFORT": "none",
        "HOSTED_LLM_JSON_MODE": "false",
    }.items():
        monkeypatch.setenv(name, value)
    s = Settings()
    assert (s.llm_provider, s.hosted_llm_base_url, s.hosted_llm_model) == (
        "openai_compatible",
        GEMINI_URL,
        "gemini-2.5-flash-lite",
    )
    assert (s.hosted_llm_reasoning_effort, s.hosted_llm_json_mode, s.hosted_llm_api_key) == ("none", False, FAKE_KEY)


def test_an_unknown_reasoning_effort_is_rejected() -> None:
    with pytest.raises(ValueError):
        gemini_settings(hosted_llm_reasoning_effort="maximum")


# ------------------------------------------------------------------ invalid keys, quotas, timeouts
@pytest.mark.parametrize(
    ("status", "meaning"),
    [
        (401, "invalid or missing API key"),
        (402, "payment required"),
        (403, "access denied"),
        (429, "rate limit or quota reached"),
    ],
)
def test_rejections_are_not_retried_and_never_echo_the_key(
    fake_http: list[dict[str, Any]],
    status: int,
    meaning: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # evaluation/run_eval.py calls logging.disable(WARNING) when test_evaluation runs it in-process. Undo it for this
    # test with logging.disable (which also clears the loggers' cached level checks) and restore it afterwards.
    previous = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        _check_rejection(fake_http, status, meaning, caplog)
    finally:
        logging.disable(previous)


def _check_rejection(
    fake_http: list[dict[str, Any]], status: int, meaning: str, caplog: pytest.LogCaptureFixture
) -> None:
    fake_http[0]["responses"].append(httpx.Response(status, json={"error": {"message": f"bad key {FAKE_KEY}"}}))
    p = OpenAICompatibleProvider(GEMINI_URL, FAKE_KEY, "m", timeout=5, max_retries=2)
    with pytest.raises(ProviderRejected) as exc:
        p.complete_json("sys", "user")
    assert len(fake_http) - 1 == 1  # one request, no retries: retrying can't fix a key or a quota
    assert str(status) in str(exc.value) and meaning in str(exc.value) and FAKE_KEY not in str(exc.value)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert f"rejected the request: openai_compatible HTTP {status}" in logged and FAKE_KEY not in logged


def test_server_errors_are_still_retried(fake_http: list[dict[str, Any]]) -> None:
    fake_http[0]["responses"].extend([httpx.Response(503), _ok()])
    p = OpenAICompatibleProvider(GEMINI_URL, FAKE_KEY, "m", timeout=5, max_retries=2)
    assert p.complete_json("sys", "user") == {"category": "laptops"} and len(fake_http) - 1 == 2


def test_timeouts_are_retried_a_bounded_number_of_times(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[float] = []

    def timing_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    real_client = httpx.Client

    def client(**kw: Any) -> httpx.Client:
        attempts.append(kw["timeout"])
        return real_client(transport=httpx.MockTransport(timing_out), **kw)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    p = OpenAICompatibleProvider(GEMINI_URL, FAKE_KEY, "m", timeout=4, max_retries=2)
    with pytest.raises(ProviderError) as exc:
        p.complete_text("sys", "user")
    assert attempts == [4, 4, 4] and isinstance(exc.value.__cause__, httpx.ReadTimeout)


def test_each_attempt_is_clipped_to_the_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    real_client = httpx.Client

    def client(**kw: Any) -> httpx.Client:
        seen.append(kw["timeout"])
        return real_client(transport=httpx.MockTransport(lambda r: _ok()), **kw)

    monkeypatch.setattr(httpx, "Client", client)
    p = OpenAICompatibleProvider(GEMINI_URL, FAKE_KEY, "m", timeout=20, max_retries=0)
    p.complete_json("sys", "user", deadline=time.monotonic() + 3)
    assert len(seen) == 1 and seen[0] <= 3  # never the 20 s default once the request has less time left
