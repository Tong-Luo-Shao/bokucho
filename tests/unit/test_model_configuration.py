from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from bokucho.cli import PROJECT_TOML_TEMPLATE
from bokucho.model import TextUnit
from bokucho.project import (
    Project,
    PromptConfig,
    TranslatorConfig,
    TranslatorInterfaceConfig,
)
from bokucho.translators.base import TranslationContext, build_prompt


def test_custom_prompt_templates_receive_project_and_batch_values() -> None:
    context = TranslationContext(
        source_lang="ja",
        target_lang="zh-Hant",
        system_prompt="From {source_lang} to {target_lang}; count={n}; {glossary_section}",
        user_prompt="Translate payload: {entries_json}",
    )

    system, user = build_prompt(
        [TextUnit(id="u1", source="こんにちは")],
        context,
        {"こんにちは": "您好"},
    )

    assert "From ja to zh-Hant; count=1" in system
    assert "こんにちは => 您好" in system
    assert "Preserve every listed token exactly as written" in system
    assert '"text": ["こんにちは"]' in user
    assert '"protected_tokens": []' in user


def test_translation_prompt_lists_engine_tokens_with_occurrence_counts() -> None:
    context = TranslationContext(
        source_lang="en",
        target_lang="zh-Hant",
        user_prompt="{entries_json}",
        engine="rpgmaker",
    )

    _, user = build_prompt(
        [TextUnit(id="u1", source=r"Hello \V[3] \V[3] \C[1]")],
        context,
        None,
    )

    assert '"token": "\\\\V[3]", "kind": "variable", "count": 2' in user
    assert '"token": "\\\\C[1]", "kind": "control", "count": 1' in user


@pytest.mark.parametrize("key", ["api_key", "authorization", "messages", "model"])
def test_request_options_reject_secrets_and_structural_arguments(key: str) -> None:
    with pytest.raises(ValidationError, match=key):
        TranslatorConfig(request_options={key: "not-allowed"})


def test_request_options_reject_nested_secret_keys() -> None:
    with pytest.raises(ValidationError, match="api_key"):
        TranslatorConfig(request_options={"extra_body": {"api_key": "not-allowed"}})


@pytest.mark.parametrize("name", ["AWS_SECRET_ACCESS_KEY", "PATH", "GITHUB_TOKEN"])
def test_api_key_env_names_only_an_api_key_variable(name: str) -> None:
    with pytest.raises(ValidationError, match="api_key_env"):
        TranslatorConfig(api_key_env=name)
    with pytest.raises(ValidationError, match="api_key_env"):
        TranslatorInterfaceConfig(
            id="i", name="n", provider="openai-compatible-api", model="m", api_key_env=name
        )


@pytest.mark.parametrize("name", [None, "", "OPENAI_API_KEY", "my_proxy_api_key"])
def test_api_key_env_accepts_api_key_variables(name: str | None) -> None:
    assert TranslatorConfig(api_key_env=name).api_key_env == name


def test_model_configuration_round_trip_preserves_project_fields(
    new_project, tmp_path: Path
) -> None:
    project = new_project("Sample")
    translator = TranslatorConfig(
        provider="local",
        model="custom-model",
        batch_size=12,
        base_url="https://model.example/v1",
        api_key_env="MODEL_API_KEY",
        request_options={"temperature": 0.2, "max_tokens": 2048},
    )
    prompts = PromptConfig(
        system="Translate {source_lang} to {target_lang}. {glossary_section}",
        user="Input batch: {entries_json}",
    )

    project.save_config(
        project.config.model_copy(update={"translator": translator, "prompts": prompts})
    )
    loaded = Project.load(project.root).config

    assert loaded.name == "Sample"
    assert loaded.engine.type == "livemaker"
    assert loaded.translator == translator
    assert loaded.prompts == prompts


def test_provider_interface_round_trip_preserves_limits_and_active_projection(new_project, 
    tmp_path: Path,
) -> None:
    project = new_project("Sample")
    translator = TranslatorConfig(
        interfaces=[
            TranslatorInterfaceConfig(
                id="gemini-main",
                name="Gemini Main",
                provider="gemini",
                model="gemini-2.5-flash",
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                rpm=15,
                tpm=250_000,
                rpd=1_500,
                temperature=0.3,
                thinking_level="low",
            ),
            TranslatorInterfaceConfig(
                id="claude-main",
                name="Claude Main",
                provider="claude",
                model="claude-sonnet-4-6",
            ),
        ],
        active_interface_id="gemini-main",
    )

    project.save_config(project.config.model_copy(update={"translator": translator}))
    loaded = Project.load(project.root).config.translator

    assert loaded == translator
    assert loaded.selected_interface().id == "gemini-main"
    assert loaded.provider == "gemini"
    assert loaded.model == "gemini-2.5-flash"
    assert loaded.selected_interface().tpm == 250_000



def _write_project(path: Path) -> None:
    path.write_text(
        PROJECT_TOML_TEMPLATE.format(
            name="x",
            source_lang="ja",
            target_lang="zh-Hant",
            engine="livemaker",
        ),
        encoding="utf-8",
    )


def test_project_save_config_reloads_written_configuration(tmp_path: Path) -> None:
    path = tmp_path / "project.toml"
    _write_project(path)
    project = Project.load(tmp_path)
    updated = project.config.model_copy(
        update={
            "name": "saved",
            "transforms": [{"type": "punctuation", "profile": "zh-Hant-TW"}],
        }
    )

    result = project.save_config(updated)

    assert result.name == "saved"
    assert result.transforms == [
        {"type": "punctuation", "profile": "zh-Hant-TW"}
    ]
    assert Project.load(tmp_path).config == result
