"""Common LLM provider interface.

Providers are *optional helpers*: they may enrich constraint extraction and polish the
summary text. They never see the database, never call tools and never mutate state.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

logger = logging.getLogger("agentcart.providers")


class ProviderError(RuntimeError):
    """Raised when a provider fails after its bounded retries."""


class ProviderRejected(ProviderError):
    """The provider refused the request: invalid key (401), no access or billing required (402/403), or quota or
    rate limit reached (429). Retrying can't help and would only spend quota, so it is raised at once."""


_REJECTED = {
    401: "invalid or missing API key",
    402: "payment required",
    403: "access denied",
    429: "rate limit or quota reached",
}


class LLMProvider(ABC):
    name: str = "base"
    #: Whether the provider actually calls a model (mock does not).
    is_llm: bool = True

    def __init__(self, timeout: float = 20.0, max_retries: int = 2) -> None:
        self.timeout = timeout
        self.max_retries = max_retries

    @abstractmethod
    def _complete(self, system: str, user: str, *, json_mode: bool, timeout: float) -> str: ...

    def complete_text(self, system: str, user: str, *, deadline: float | None = None) -> str:
        return self._with_retries(system, user, json_mode=False, deadline=deadline)

    def complete_json(self, system: str, user: str, *, deadline: float | None = None) -> dict[str, Any]:
        raw = self._with_retries(system, user, json_mode=True, deadline=deadline)
        return parse_json_object(raw)

    def _with_retries(self, system: str, user: str, *, json_mode: bool, deadline: float | None) -> str:
        """Bounded retries with backoff. `deadline` is a time.monotonic() value; each attempt's
        timeout is clipped so the whole call never outlives the agent's deadline."""
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            timeout = self.timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 1.0:
                    raise ProviderError(f"{self.name}: agent deadline reached") from last
                timeout = min(timeout, remaining)
            try:
                return self._complete(system, user, json_mode=json_mode, timeout=timeout)
            except ProviderRejected as exc:
                logger.warning("%s rejected the request: %s", self.name, exc)  # status and meaning only
                raise  # not retried: see ProviderRejected
            except (httpx.HTTPError, ProviderError, KeyError, ValueError, TypeError) as exc:
                last = exc
                logger.warning("%s attempt %d failed: %s", self.name, attempt + 1, type(exc).__name__)
                if attempt < self.max_retries:
                    time.sleep(min(2.0, 0.4 * (2**attempt)))
        raise ProviderError(f"{self.name} failed after {self.max_retries + 1} attempts") from last

    def _post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        with httpx.Client(timeout=timeout or self.timeout) as client:
            resp = client.post(url, json=payload, headers=headers or {})
        if resp.status_code in _REJECTED:
            # The status only: never the response body (it can echo prompts) or the key.
            raise ProviderRejected(f"{self.name} HTTP {resp.status_code}: {_REJECTED[resp.status_code]}")
        if resp.status_code >= 400:
            # Do not log response bodies: they can echo prompts.
            raise ProviderError(f"{self.name} HTTP {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data


def parse_json_object(raw: str) -> dict[str, Any]:
    """Extract the first JSON object from model output (tolerates code fences)."""
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    obj = json.loads(raw[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj
