from __future__ import annotations

import json

import pytest

from bokucho.infrastructure import model_catalog
from bokucho.infrastructure.model_catalog import fetch_provider_models
from bokucho.project import TranslatorInterfaceConfig


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _openai_list(*ids: str) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 1715367049, "owned_by": "system"}
            for model in ids
        ],
    }


def _gemini_model(name: str) -> dict:
    return {
        "name": f"models/{name}",
        "version": "001",
        "displayName": name,
        "inputTokenLimit": 8192,
        "outputTokenLimit": 16384,
        "supportedGenerationMethods": ["countTokens", "generateContent"],
    }


def _fetch(monkeypatch: pytest.MonkeyPatch, provider: str, payload: dict) -> tuple[str, ...]:
    monkeypatch.setattr(model_catalog, "urlopen", lambda request, timeout: _Response(payload))
    interface = TranslatorInterfaceConfig(
        id="test",
        name=provider,
        provider=provider,
        model="any",
        base_url="https://models.example/v1",
    )
    return fetch_provider_models(interface, "secret")


@pytest.mark.parametrize(
    ("provider", "payload", "expected"),
    [
        (
            "openai",
            _openai_list(
                "gpt-4o-mini",
                "tts-1",
                "gpt-4o-mini-tts",
                "whisper-1",
                "gpt-4o-transcribe",
                "gpt-audio",
                "gpt-realtime",
                "gpt-live-1",
                "text-embedding-3-small",
                "omni-moderation-latest",
                "dall-e-3",
                "gpt-image-1",
                "sora-2",
            ),
            ("gpt-4o-mini",),
        ),
        (
            "gemini",
            {
                "models": [
                    _gemini_model("gemini-2.5-flash"),
                    _gemini_model("gemini-2.5-flash-preview-tts"),
                    _gemini_model("gemini-2.5-flash-image"),
                    _gemini_model("nano-banana-pro-preview"),
                    _gemini_model("lyria-3-pro-preview"),
                ]
            },
            ("gemini-2.5-flash",),
        ),
        (
            "openrouter",
            _openai_list(
                "openai/gpt-4o-mini",
                "openrouter/auto",
                "google/gemini-3-pro-image-preview",
                "openai/gpt-audio",
                "google/lyria-3-clip-preview",
            ),
            ("openai/gpt-4o-mini", "openrouter/auto"),
        ),
        (
            "openai-compatible-api",
            _openai_list(
                "Qwen/Qwen3-8B",
                "IndexTeam/IndexTTS-2",
                "fnlp/MOSS-TTSD-v0.5",
                "TeleAI/TeleSpeechASR",
                "minimax/speech-02-hd",
                "FunAudioLLM/CosyVoice2-0.5B",
                "Qwen/Qwen3-Reranker-8B",
                "BAAI/bge-m3",
                "black-forest-labs/FLUX.1-schnell",
                "stabilityai/stable-diffusion-3-5-large",
                "deepseek-ai/DeepSeek-OCR",
                "bytedance/seedance-2.0/text-to-video",
                "veo-3.1-generate-preview",
            ),
            ("Qwen/Qwen3-8B",),
        ),
        (
            "local",
            _openai_list("qwen3:8b", "nomic-embed-text:latest", "all-minilm:latest"),
            ("qwen3:8b",),
        ),
    ],
)
def test_non_text_models_are_left_out_of_the_fetched_list(
    monkeypatch: pytest.MonkeyPatch, provider: str, payload: dict, expected: tuple[str, ...]
) -> None:
    assert _fetch(monkeypatch, provider, payload) == expected


def test_a_model_whose_name_only_holds_a_marker_inside_a_word_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _openai_list("mattshumer/reflection-70b", "openai/gpt-4o-mini")

    assert _fetch(monkeypatch, "openrouter", payload) == (
        "mattshumer/reflection-70b",
        "openai/gpt-4o-mini",
    )
