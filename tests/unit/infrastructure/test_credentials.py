from __future__ import annotations

import base64
import json
from pathlib import Path

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.infrastructure import factories
from bokucho.infrastructure.credentials import (
    Base64CredentialStore,
    credential_account,
    default_credentials_path,
)
from bokucho.project import Project, TranslatorConfig, TranslatorInterfaceConfig


def test_credential_account_is_deterministic() -> None:
    assert credential_account("Claude", None) == "claude|default"
    assert credential_account("LOCAL", "HTTPS://API.EXAMPLE/V1/") == (
        "local|https://api.example/v1"
    )


def test_base64_store_round_trip_never_writes_plaintext(tmp_path: Path) -> None:
    path = tmp_path / "Resource" / "credentials.json"
    store = Base64CredentialStore(path)

    store.add("openrouter", "https://openrouter.ai/api/v1", "first-key")
    store.add("openrouter", "https://openrouter.ai/api/v1", "second-key")

    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    account = credential_account("openrouter", "https://openrouter.ai/api/v1")
    assert "first-key" not in raw
    assert "second-key" not in raw
    assert payload == {
        "version": 1,
        "accounts": {
            account: [
                base64.b64encode(b"first-key").decode("ascii"),
                base64.b64encode(b"second-key").decode("ascii"),
            ]
        },
    }
    assert store.get_all("openrouter", "https://openrouter.ai/api/v1") == (
        "first-key",
        "second-key",
    )


def test_base64_store_does_not_add_duplicate_api_keys(tmp_path: Path) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")

    store.add("deepseek", "https://api.deepseek.com", "same-key")
    store.add("deepseek", "https://api.deepseek.com", "same-key")

    assert store.get_all("deepseek", "https://api.deepseek.com") == ("same-key",)


def test_default_credentials_path_uses_portable_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BOKUCHO_PORTABLE_ROOT", str(tmp_path))

    assert default_credentials_path() == tmp_path / "Resource" / "credentials.json"


def test_translator_factory_passes_explicit_secret(tmp_path: Path, monkeypatch) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path, "keys", "ja", "zh-Hant", "livemaker")
    )
    captured = {}
    translator = object()

    def get_fake_translator(name, **kwargs):
        captured.update(name=name, **kwargs)
        return translator

    monkeypatch.setattr(factories, "get_translator", get_fake_translator)

    assert factories.make_translator(project, "credential-secret") is translator
    assert captured["api_key"] == "credential-secret"


def test_translator_runtime_reads_shared_provider_endpoint_keys(
    tmp_path: Path, monkeypatch
) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "shared", "ja", "en", "livemaker")
    )
    endpoint = "https://models.example/v1"
    interface = TranslatorInterfaceConfig(
        id="project-specific-id",
        name="Shared endpoint",
        provider="openai-compatible-api",
        model="model-a",
        base_url=endpoint,
    )
    project.save_config(project.config.model_copy(update={
        "translator": TranslatorConfig(
            interfaces=[interface],
            active_interface_id=interface.id,
        ),
    }))
    store = Base64CredentialStore(tmp_path / "credentials.json")
    store.add(interface.provider, endpoint, "shared-key")
    monkeypatch.setattr(factories, "default_credential_store", lambda: store)

    assert factories.stored_secrets(Project.load(project.root)) == ("shared-key",)
