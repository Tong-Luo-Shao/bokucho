"""Translation providers — plugin seam #2."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from bokucho.translators.base import TranslationContext, Translator


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str | None
    translator_path: str
    api_key_env: str


_OPENAI_TRANSLATOR = "bokucho.translators.local:LocalTranslator"
_CLAUDE_TRANSLATOR = "bokucho.translators.claude:ClaudeTranslator"

PROVIDER_PRESETS = (
    ProviderPreset(
        "gemini",
        "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        _OPENAI_TRANSLATOR,
        "GEMINI_API_KEY",
    ),
    ProviderPreset(
        "claude",
        "Claude",
        "https://api.anthropic.com",
        _CLAUDE_TRANSLATOR,
        "ANTHROPIC_API_KEY",
    ),
    ProviderPreset(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        _OPENAI_TRANSLATOR,
        "OPENAI_API_KEY",
    ),
    ProviderPreset(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        _OPENAI_TRANSLATOR,
        "OPENAI_API_KEY",
    ),
    ProviderPreset(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com",
        _OPENAI_TRANSLATOR,
        "OPENAI_API_KEY",
    ),
    ProviderPreset(
        "openai-compatible-api",
        "OpenAI-compatible API",
        None,
        _OPENAI_TRANSLATOR,
        "OPENAI_API_KEY",
    ),
    ProviderPreset(
        "local",
        "Local",
        "http://localhost:11434/v1",
        _OPENAI_TRANSLATOR,
        "OPENAI_API_KEY",
    ),
)

def available_translators() -> list[str]:
    """Names of all registered translation providers."""
    return [preset.key for preset in PROVIDER_PRESETS]


def get_provider_preset(name: str) -> ProviderPreset:
    """Return the UI/runtime preset registered under ``name``."""
    try:
        return next(preset for preset in PROVIDER_PRESETS if preset.key == name)
    except StopIteration:
        raise ValueError(
            f"Unknown translator: {name!r}. Available: {', '.join(available_translators())}"
        ) from None


def provider_default_base_url(name: str) -> str | None:
    """Return the default API endpoint for a provider preset."""
    return get_provider_preset(name).base_url


def provider_default_api_key_env(name: str) -> str:
    """Return the environment fallback used by the provider implementation."""
    return get_provider_preset(name).api_key_env


def get_translator(name: str, **kwargs) -> Translator:
    """Instantiate the provider registered under ``name`` (kwargs -> constructor)."""
    module_path, _, class_name = get_provider_preset(name).translator_path.partition(":")
    return getattr(importlib.import_module(module_path), class_name)(**kwargs)


__all__ = [
    "PROVIDER_PRESETS",
    "ProviderPreset",
    "TranslationContext",
    "Translator",
    "available_translators",
    "get_provider_preset",
    "get_translator",
    "provider_default_api_key_env",
    "provider_default_base_url",
]
