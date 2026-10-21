"""HTTP providers: Ollama (local), any OpenAI-compatible endpoint, and Anthropic."""

from __future__ import annotations

from app.providers.base import LLMProvider, ProviderError


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, *, timeout: float = 20.0, max_retries: int = 2) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
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
    """Works with Google Gemini's OpenAI-compatible endpoint, Groq, OpenRouter, Together, vLLM, LM Studio, Ollama's
    /v1, etc.

    `reasoning_effort` is sent only when set (Gemini 2.5 models "think" by default, which can use up the reply
    budget; "none" turns that off). `json_mode=False` asks for JSON in the prompt instead of `response_format`, for
    endpoints that don't accept `{"type": "json_object"}`; the reply is parsed the same way either way."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 20.0,
        max_retries: int = 2,
        reasoning_effort: str = "",
        json_mode: bool = True,
    ) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
        if not api_key:
            raise ProviderError("HOSTED_LLM_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.json_mode = json_mode

    def _complete(self, system: str, user: str, *, json_mode: bool, timeout: float) -> str:
        payload: dict[str, object] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 600,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if json_mode and self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        elif json_mode:
            payload["messages"] = [
                {"role": "system", "content": system + "\nRespond with a single JSON object only, no prose."},
                {"role": "user", "content": user},
            ]
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        data = self._post(
            f"{self.base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise ProviderError("unexpected response shape")
        if not content.strip():
            # e.g. a "thinking" model spent the whole token budget before answering
            raise ProviderError("empty response")
        return content


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, *, timeout: float = 20.0, max_retries: int = 2) -> None:
        super().__init__(timeout=timeout, max_retries=max_retries)
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
