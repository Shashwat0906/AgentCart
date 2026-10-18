"""Deterministic provider used by default — no network, no API key."""

from __future__ import annotations

from typing import Any

from app.providers.base import LLMProvider


class MockProvider(LLMProvider):
    name = "mock"
    is_llm = False

    def _complete(self, system: str, user: str, *, json_mode: bool, timeout: float) -> str:
        # The rule-based parser already extracted everything; the mock adds nothing.
        return "{}" if json_mode else ""

    def complete_json(self, system: str, user: str, *, deadline: float | None = None) -> dict[str, Any]:
        return {}
