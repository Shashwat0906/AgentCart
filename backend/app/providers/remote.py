"""HTTP providers: Ollama (local), any OpenAI-compatible endpoint, and Anthropic."""

from __future__ import annotations

from app.providers.base import LLMProvider, ProviderError


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, **kw: float | int) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _complete(self, system: str, user: str, *, json_mode: bool, timeout: float) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 600},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["format"] = "json"
        data = self._post(f"{self.base_url}/api/chat", payload, timeout=timeout)
        content = data["message"]["content"]
        if not isinstance(content, str):
            raise ProviderError("unexpected Ollama response")
        return content


class OpenAICompatibleProvider(LLMProvider):
    """Works with Groq, OpenRouter, Together, vLLM, LM Studio, Ollama's /v1, etc."""

    name = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, model: str, **kw: float | int) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        if not api_key:
            raise ProviderError("HOSTED_LLM_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _complete(self, system: str, user: str, *, json_mode: bool, timeout: float) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 600,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(
            f"{self.base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ProviderError("unexpected response shape")
        return content


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, **kw: float | int) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self.api_key = api_key
        self.model = model

    def _complete(self, system: str, user: str, *, json_mode: bool, timeout: float) -> str:
        if json_mode:
            system += "\nRespond with a single JSON object only, no prose."
        data = self._post(
            "https://api.anthropic.com/v1/messages",
            {
                "model": self.model,
                "max_tokens": 600,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            timeout=timeout,
        )
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        if not parts:
            raise ProviderError("empty Anthropic response")
        return "".join(parts)
