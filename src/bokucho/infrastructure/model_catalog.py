"""Provider model discovery for interface editing and connection tests."""

from __future__ import annotations

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from bokucho.application.errors import MissingCredential, ProviderFailure
from bokucho.project import TranslatorInterfaceConfig
from bokucho.translators import (
    provider_default_api_key_env,
    provider_default_base_url,
)

_ANTHROPIC_VERSION = "2023-06-01"
_TIMEOUT_SECONDS = 20
_NON_TEXT_MARKERS = (
    "tts", "asr", "whisper", "transcribe", "speech", "voice", "audio",
    "realtime", "live", "lyria",
    "embed", "rerank", "moderation", "bge", "minilm",
    "image", "dall-e", "banana", "flux", "diffusion", "ocr", "video", "sora", "veo",
)
_NON_TEXT = "|".join(_NON_TEXT_MARKERS)
_NON_TEXT_MODEL = re.compile(
    rf"(?<![a-z])(?:{_NON_TEXT})|(?:{_NON_TEXT})(?![a-z])", re.IGNORECASE
)


def fetch_provider_models(
    interface: TranslatorInterfaceConfig,
    secret: str | None,
) -> tuple[str, ...]:
    """Fetch model identifiers using the provider's documented list endpoint."""
    key = secret or os.environ.get(
        interface.api_key_env or provider_default_api_key_env(interface.provider)
    )
    if not key and interface.provider != "local":
        raise MissingCredential(
            f"No API key is available for interface {interface.name!r}."
        )

    base_url = interface.base_url or provider_default_base_url(interface.provider)
    if not base_url:
        raise ProviderFailure(
            f"Interface {interface.name!r} requires a Base URL before models can be fetched."
        )

    if interface.provider == "gemini":
        payload = _fetch_gemini_models(base_url, key or "")
        models = (
            str(item.get("name", "")).removeprefix("models/")
            for item in payload.get("models", [])
            if "generateContent" in item.get("supportedGenerationMethods", [])
        )
    else:
        url, headers = _model_request(interface.provider, base_url, key or "")
        payload = _request_json(url, headers)
        models = (
            str(item.get("id", ""))
            for item in payload.get("data", [])
            if isinstance(item, dict)
        )

    unique = tuple(
        sorted({model for model in models if model and not _NON_TEXT_MODEL.search(model)})
    )
    if not unique:
        raise ProviderFailure(
            f"The {interface.provider} model endpoint returned no usable models."
        )
    return unique


def _fetch_gemini_models(base_url: str, key: str) -> dict:
    root = base_url.rstrip("/")
    if root.endswith("/openai"):
        root = root[: -len("/openai")]
    return _request_json(f"{root}/models?key={quote(key, safe='')}", {})


def _model_request(
    provider: str,
    base_url: str,
    key: str,
) -> tuple[str, dict[str, str]]:
    root = base_url.rstrip("/")
    if provider == "claude":
        url = f"{root}/models" if root.endswith("/v1") else f"{root}/v1/models"
        return url, {
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
        }
    return f"{root}/models", {"Authorization": f"Bearer {key}"}


def _request_json(url: str, headers: dict[str, str]) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Bokucho",
            **headers,
        },
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ProviderFailure(
            f"Model discovery failed with HTTP {error.code}."
        ) from None
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderFailure(
            f"Model discovery failed ({type(error).__name__})."
        ) from None
    if not isinstance(payload, dict):
        raise ProviderFailure("Model discovery returned an invalid response.")
    return payload
