"""Infrastructure factories shared by presentation layers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from bokucho.application.contracts import RasterCodec
from bokucho.application.errors import InvalidProject, MissingDependency
from bokucho.application.translation_limits import (
    PromptTokenCounter,
    RequestLimiter,
    TranslationLane,
)
from bokucho.engines import get_adapter, is_rpgmaker
from bokucho.engines.base import EngineAdapter
from bokucho.infrastructure.credentials import (
    default_credential_store,
    shared_provider_credentials,
)
from bokucho.infrastructure.rate_limits import PersistentRequestLimiter
from bokucho.infrastructure.tokens import TiktokenPromptCounter
from bokucho.project import UNPACKED_DIRNAME, Project, TranslatorInterfaceConfig
from bokucho.transform import get_transform
from bokucho.transform.base import Transform
from bokucho.translate.glossary import global_terms_path
from bokucho.translators import (
    get_translator,
    provider_default_api_key_env,
    provider_default_base_url,
)
from bokucho.translators.base import Translator


def make_engine_adapter(project: Project) -> EngineAdapter:
    """Construct the engine configured by ``project``."""
    kwargs = {}
    if project.config.engine.type == "livemaker":
        kwargs["proxy_map"] = project.root / project.config.engine.proxy_map
        kwargs["workspace"] = project.root / UNPACKED_DIRNAME
    elif is_rpgmaker(project.config.engine.type):
        kwargs["workspace"] = project.root / UNPACKED_DIRNAME
    elif project.config.engine.type == "renpy":
        from bokucho.engines.renpy import default_language
        from bokucho.user_settings import UserSettingsStore

        raw_sdk = project.config.engine.sdk_path or UserSettingsStore().load().renpy_sdk_path
        if raw_sdk:
            sdk = Path(raw_sdk)
            kwargs["sdk_path"] = sdk if sdk.is_absolute() else project.root / sdk
        kwargs["language"] = project.config.engine.language or default_language(
            project.config.target_lang
        )
    elif project.config.engine.type == "cs2":
        kwargs["source_lang"] = project.config.source_lang
        kwargs["target_lang"] = project.config.target_lang
        kwargs["prefill_lang"] = project.config.engine.prefill_lang
    elif project.config.engine.type == "unity":
        kwargs["source_lang"] = project.config.source_lang
        kwargs["target_lang"] = project.config.target_lang
        kwargs["text_files"] = tuple(project.config.engine.text_files or ())
    try:
        return get_adapter(project.config.engine.type, **kwargs)
    except ValueError as exc:
        raise InvalidProject(str(exc)) from exc
    except ImportError as exc:
        raise MissingDependency(str(exc)) from exc


def make_image_codec() -> RasterCodec:
    """Construct the raster codec used by image localization workflows."""
    from bokucho.infrastructure.pillow_image_codec import PillowImageCodec

    return PillowImageCodec()


def _request_options(config: TranslatorInterfaceConfig) -> dict[str, object]:
    request_options = dict(config.request_options)
    if config.temperature is not None:
        request_options["temperature"] = config.temperature
    if config.thinking_level is None:
        return request_options
    if config.provider == "claude":
        request_options["output_config"] = {
            **request_options.get("output_config", {}),
            "effort": config.thinking_level,
        }
    else:
        request_options["reasoning_effort"] = config.thinking_level
    return request_options


def make_translator(project: Project, secret: str | None = None) -> Translator:
    """Construct the translator configured by ``project``."""
    config = project.config.translator.selected_interface()
    try:
        kwargs: dict[str, object] = {
            "model": config.model,
            "request_options": _request_options(config),
            "api_key": secret or None,
            "api_key_env": (
                provider_default_api_key_env(config.provider)
                if config.api_key_env is None
                else config.api_key_env
            ),
        }
        endpoint = config.base_url or provider_default_base_url(config.provider)
        if endpoint:
            kwargs["base_url"] = endpoint
        return get_translator(config.provider, **kwargs)
    except ValueError as exc:
        raise InvalidProject(str(exc)) from exc
    except ImportError as exc:
        raise MissingDependency(str(exc)) from exc


def make_prompt_token_counter(project: Project) -> PromptTokenCounter:
    """Construct the tiktoken counter for the active translator model."""
    return TiktokenPromptCounter(project.config.translator.selected_interface().model)


def make_request_limiter(
    project: Project, lane: int | None = None
) -> RequestLimiter | None:
    """Construct persistent quota enforcement for the active interface."""
    interface = project.config.translator.selected_interface()
    if not (interface.rpm or interface.tpm or interface.rpd):
        return None
    return PersistentRequestLimiter(
        project.root / ".bokucho" / "translation_usage.json",
        interface_id=interface.id if lane is None else f"{interface.id}#{lane}",
        rpm=interface.rpm,
        tpm=interface.tpm,
        rpd=interface.rpd,
    )


def stored_secrets(project: Project) -> tuple[str, ...]:
    """Every API key shared by the active translator's provider and endpoint."""
    config = project.config.translator.selected_interface()
    store = default_credential_store()
    endpoint = config.base_url or provider_default_base_url(config.provider)
    return shared_provider_credentials(
        store,
        config.provider,
        endpoint,
        config.id,
    )


def make_translation_lanes(
    project: Project,
    secrets: Sequence[str] | None = None,
) -> tuple[TranslationLane, ...]:
    """One lane per stored credential, each with its own client and quota."""
    if secrets is None:
        secrets = stored_secrets(project)
    keys = tuple(key for key in secrets if key) or (None,)
    return tuple(
        TranslationLane(
            translator=make_translator(project, secret=key),
            limiter=make_request_limiter(project, lane=index),
        )
        for index, key in enumerate(keys)
    )


def make_transform_pipeline(
    project: Project, specs: Sequence[Mapping[str, Any]] | None = None
) -> tuple[Transform, ...]:
    """The ordered pipeline ``specs`` describe, by default the ones ``project`` configures."""
    if specs is None:
        specs = project.config.transforms
        if not specs:
            raise InvalidProject("No [[transform]] configured in project.toml.")
    global_terms = global_terms_path()
    pipeline: list[Transform] = []
    try:
        for spec in specs:
            kwargs = {key: value for key, value in spec.items() if key != "type"}
            if "custom_dict" in kwargs:
                path = Path(str(kwargs["custom_dict"]))
                kwargs["custom_dict"] = (
                    path if path.is_absolute() else project.root / path
                )
            if (
                spec.get("type") == "opencc"
                and "base_dict" not in kwargs
                and global_terms.is_file()
            ):
                kwargs["base_dict"] = global_terms
            pipeline.append(get_transform(str(spec["type"]), **kwargs))
    except (KeyError, ValueError, ImportError, OSError) as exc:
        raise InvalidProject(f"Bad [[transform]] config: {exc}") from exc
    return tuple(pipeline)
