"""Provider factory. Falls back to the mock provider if a configured provider is unusable."""

from __future__ import annotations

import logging

from app.core.config import Settings
from app.providers.base import LLMProvider, ProviderError
from app.providers.mock import MockProvider
from app.providers.remote import AnthropicProvider, OllamaProvider, OpenAICompatibleProvider

logger = logging.getLogger("agentcart.providers")

__all__ = ["LLMProvider", "MockProvider", "ProviderError", "build_provider"]


def build_provider(settings: Settings) -> LLMProvider:
    timeout, retries = settings.llm_timeout_seconds, settings.llm_max_retries
    try:
        if settings.llm_provider == "ollama":
            return OllamaProvider(settings.ollama_base_url, settings.ollama_model, timeout=timeout, max_retries=retries)
        if settings.llm_provider == "openai_compatible":
            return OpenAICompatibleProvider(
                settings.hosted_llm_base_url,
                settings.hosted_llm_api_key,
                settings.hosted_llm_model,
                timeout=timeout,
                max_retries=retries,
                reasoning_effort=settings.hosted_llm_reasoning_effort,
                json_mode=settings.hosted_llm_json_mode,
            )
        if settings.llm_provider == "anthropic":
            return AnthropicProvider(
                settings.anthropic_api_key, settings.anthropic_model, timeout=timeout, max_retries=retries
            )
    except ProviderError as exc:
        logger.warning("LLM provider %s unavailable (%s); using mock", settings.llm_provider, exc)
    return MockProvider(timeout=timeout, max_retries=retries)
