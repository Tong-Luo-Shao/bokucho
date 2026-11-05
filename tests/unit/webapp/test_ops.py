"""RED/GREEN tests for the job registry, settings, and credential methods."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event

import pytest

from bokucho.application.projects import CreateProjectRequest, create_project, string_filename
from bokucho.application.translation_limits import TranslationLane
from bokucho.infrastructure.credentials import Base64CredentialStore, interface_credential_scope
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.translators import provider_default_base_url
from bokucho.webapp import registry as registry_module
from bokucho.webapp.api import Api
from bokucho.webapp.registry import JOBS

from .conftest import FakeWindow

_PREFIX = "bokucho.onEvent("


@pytest.fixture
def registry() -> dict:
    return dict(JOBS)


@pytest.fixture
def credential_store(tmp_path: Path) -> Base64CredentialStore:
    return Base64CredentialStore(path=tmp_path / "credentials.json")


@pytest.fixture
def api(app_state, event_bus, registry: dict, credential_store: Base64CredentialStore) -> Api:
    return Api(app_state, event_bus, registry=registry, credential_store=credential_store)


def _seed_project(
    api: Api, tmp_path: Path, name: str, string_files: list[StringFile]
) -> Project:
    destination = tmp_path / name
    created = api.create_project(
        name=name,
        engine="livemaker",
        destination=str(destination),
        source_lang="ja",
        target_lang="zh-Hant",
    )
    assert "error" not in created
    project = Project.load(created["root"])
    for string_file in string_files:
        project.save_string_file(string_file, string_filename(string_file.source_file))
    return Project.load(project.root)


def _pushed_events(fake_window: FakeWindow) -> list[dict]:
    return [json.loads(call[len(_PREFIX) : -1]) for call in fake_window.calls]


def _wait_for_terminal(fake_window: FakeWindow, job: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in _pushed_events(fake_window):
            if event["job"] == job and event["type"] in ("succeeded", "failed", "cancelled"):
                return event
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for a terminal event for job {job!r}")


def test_quality_reports_the_missing_variable_issue(api: Api, tmp_path: Path) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "qa",
        [
            StringFile(
                source_file="a.lsb",
                units=[TextUnit(id="u1", source="Hello %name%", target="Hello")],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    (issue,) = api.quality()["issues"]
    assert issue["unit_id"] == "u1"
    assert (issue["code"], issue["params"]) == ("variables_missing", {"missing": "%name%"})
    assert issue["message"] == "missing variables: %name%"


def test_quality_reports_line_count_as_nonblocking_warning(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "qa-line-count",
        [
            StringFile(
                source_file="a.lsb",
                units=[TextUnit(id="u1", source="一行\n二行", target="合併")],
            )
        ],
    )
    assert "error" not in api.open_project(str(project.root))

    report = api.quality()
    (warning,) = report["issues"]
    assert warning["check"] == "line-count"
    assert warning["severity"] == "warning"
    assert (warning["code"], warning["params"]) == ("line_count", {"source": 2, "target": 1})
    assert report["passed"] is True


@pytest.mark.usefixtures("no_rehearsal")
def test_build_patch_job_produces_the_patcher_in_the_dist_directory(
    api: Api, tmp_path: Path, fake_window: FakeWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bokucho.engines.base import EngineAdapter
    from bokucho.project import FontSourceConfig

    class TextOnlyAdapter(EngineAdapter):
        def extract(self, game_path, sink=None, token=None):
            return []

        def inject(self, units, game_path, out_path, sink=None, token=None):
            raise AssertionError("a LiveMaker patch is compiled on the player's machine")

    project = _seed_project(
        api,
        tmp_path,
        "patch",
        [StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="hi", target="嗨")])],
    )
    (project.root / "Game.exe").write_bytes(b"MZ original game")
    (project.root / "font.ttf").write_bytes(b"font source")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": "Game.exe"}),
                "font": project.config.font.model_copy(
                    update={"sources": [FontSourceConfig(path="font.ttf")]}
                ),
            }
        )
    )
    stub = tmp_path / "bokucho-patcher.exe"
    stub.write_bytes(b"MZ stub")
    monkeypatch.setattr(registry_module, "make_engine_adapter", lambda current: TextOnlyAdapter())
    monkeypatch.setattr("bokucho.application.release.patcher_stub", lambda: stub)
    assert "error" not in api.open_project(str(project.root))

    result = api.start_job("build-patch")
    assert "error" not in result

    event = _wait_for_terminal(fake_window, "build-patch")
    api.jobs.join(timeout=5)
    assert event["type"] == "succeeded", event
    patcher = Path(event["detail"]["patcher"])
    assert patcher.is_file()
    assert patcher == project.root / "dist" / "patch-ja2zh-tw-patcher.exe"


def test_inject_job_reports_the_injected_count_and_out_directory(
    api: Api,
    tmp_path: Path,
    fake_window: FakeWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "inject",
        [
            StringFile(
                source_file="a.lsb",
                units=[
                    TextUnit(
                        id="u1", source="hi", target="嗨", engine_ref={"lsb": "a.lsb"}
                    )
                ],
            )
        ],
    )
    (project.root / "game").mkdir()
    engine = project.config.engine.model_copy(update={"game_path": "game"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    project = Project.load(project.root)
    assert "error" not in api.open_project(str(project.root))

    class _FakeAdapter:
        name = "fake"

        def extract(self, game_path, sink, token):
            return []

        def inject(self, units, game_path, out_path, sink, token):
            out_path.mkdir(parents=True, exist_ok=True)
            (out_path / "marker.txt").write_text("done", encoding="utf-8")

    monkeypatch.setattr(registry_module, "make_engine_adapter", lambda project: _FakeAdapter())

    result = api.start_job("inject")
    assert "error" not in result

    event = _wait_for_terminal(fake_window, "inject")
    api.jobs.join(timeout=5)
    assert event["type"] == "succeeded"
    assert event["detail"]["translated"] == 1
    out_dir = Path(event["detail"]["out_dir"])
    assert out_dir == project.root / "out"
    assert (out_dir / "marker.txt").is_file()


def _drafts_held_before_todays_checks() -> list[StringFile]:
    return [
        StringFile(
            source_file="a.lsb",
            units=[
                TextUnit(id="u1", source="熱いっ！", target="好燙っ！", status=Status.MT),
                TextUnit(
                    id="u2",
                    source="古い鍵が壊れた。",
                    target="舊鑰匙ки壞了。",
                    status=Status.MT,
                ),
                TextUnit(id="u3", source="こんにちは", target="你好", status=Status.MT),
            ],
        )
    ]


def test_recheck_preview_counts_what_would_move_and_moves_nothing(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(api, tmp_path, "recheck", _drafts_held_before_todays_checks())
    assert "error" not in api.open_project(str(project.root))

    assert api.recheck_preview() == {"settled": 1, "sent_back": 1}
    saved = Project.load(project.root).load_string_files()[0].units
    assert [unit.target for unit in saved] == ["好燙っ！", "舊鑰匙ки壞了。", "你好"]


def test_recheck_job_reports_what_it_settled_and_sent_back(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "recheck", _drafts_held_before_todays_checks())
    assert "error" not in api.open_project(str(project.root))

    assert "error" not in api.start_job("recheck")

    event = _wait_for_terminal(fake_window, "recheck")
    api.jobs.join(timeout=5)
    assert event["type"] == "succeeded"
    assert event["detail"] == {"settled": 1, "sent_back": 1}
    assert api.recheck_preview() == {"settled": 0, "sent_back": 0}


def test_proxy_map_job_reports_the_mapping_path(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "proxy",
        [StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="hi", target="😀")])],
    )
    assert "error" not in api.open_project(str(project.root))

    result = api.start_job("proxy-map")
    assert "error" not in result

    event = _wait_for_terminal(fake_window, "proxy-map")
    api.jobs.join(timeout=5)
    assert event["type"] == "succeeded"
    path = Path(event["detail"]["path"])
    assert path.is_file()
    assert path == project.root / "proxy_map.json"


def test_translate_job_registers_secrets_before_returning_and_reports_counts(
    api: Api,
    tmp_path: Path,
    fake_window: FakeWindow,
    monkeypatch: pytest.MonkeyPatch,
    credential_store: Base64CredentialStore,
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "translate",
        [StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="こんにちは")])],
    )
    assert "error" not in api.open_project(str(project.root))
    secret = "sk-translate-secret"
    config = project.config.translator
    endpoint = config.base_url or provider_default_base_url(config.provider)
    credential_store.add(config.provider, endpoint, secret)

    class _FakeTranslator:
        name = "fake"

        def translate(self, batch, context, glossary=None):
            return ["嗨" for _ in batch]

    monkeypatch.setattr(registry_module, "default_credential_store", lambda: credential_store)
    monkeypatch.setattr(
        registry_module,
        "make_translation_lanes",
        lambda project, secrets=None: (TranslationLane(_FakeTranslator()),),
    )

    result = api.start_job("translate")
    assert "error" not in result

    assert api.jobs._crash_logger.redactor.redact(secret) == "[REDACTED]"

    event = _wait_for_terminal(fake_window, "translate")
    api.jobs.join(timeout=5)
    assert event["type"] == "succeeded"
    assert event["detail"]["translated"] == 1


def test_save_translator_settings_persists_provider_and_model(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(api, tmp_path, "settings", [])
    assert "error" not in api.open_project(str(project.root))

    result = api.save_translator_settings(provider="openai", model="gpt-5")

    assert "error" not in result
    reloaded = Project.load(project.root)
    assert reloaded.config.translator.provider == "openai"
    assert reloaded.config.translator.model == "gpt-5"


def _open_project(api: Api, tmp_path: Path) -> Project:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "Interfaces", "ja", "zh-Hant", "livemaker")
    )
    assert "error" not in api.open_project(str(project.root))
    return project


def test_provider_interfaces_are_saved_and_activated_independently(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    api = Api(app_state, event_bus, credential_store=store)
    project = _open_project(api, tmp_path)

    gemini = api.save_provider_interface(
        {
            "name": "Gemini production",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
        "gemini-secret",
    )
    claude = api.save_provider_interface(
        {
            "name": "Claude review",
            "provider": "claude",
            "model": "claude-sonnet-4-6",
        },
        "claude-secret",
    )

    assert "error" not in gemini
    assert "error" not in claude
    gemini_id = gemini["interface"]["id"]
    claude_id = claude["interface"]["id"]
    activated = api.activate_provider_interface(claude_id)
    assert "error" not in activated

    loaded = Project.load(project.root).config.translator
    assert {item.id for item in loaded.interfaces}.issuperset({gemini_id, claude_id})
    assert loaded.active_interface_id == claude_id
    assert store.get_all(
        "gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"
    ) == ("gemini-secret",)
    assert store.get_all("claude", "https://api.anthropic.com") == ("claude-secret",)


def test_interface_concurrency_is_saved_with_its_parameters_and_bounded(
    app_state, event_bus, tmp_path: Path
) -> None:
    api = Api(
        app_state,
        event_bus,
        credential_store=Base64CredentialStore(tmp_path / "credentials.json"),
    )
    project = _open_project(api, tmp_path)
    saved = api.save_provider_interface(
        {"name": "Gemini", "provider": "gemini", "model": "gemini-2.5-flash"},
        "gemini-secret",
    )
    interface = saved["interface"]
    assert interface["concurrency"] == 1

    updated = api.update_provider_interface_parameters(
        interface["id"], 0, 0, 0, concurrency=4
    )
    assert updated["interface"]["concurrency"] == 4
    loaded = Project.load(project.root).config.translator
    assert next(item for item in loaded.interfaces if item.id == interface["id"]).concurrency == 4

    api.update_provider_interface_parameters(interface["id"], 10, 0, 0)
    for bad in (0, 17):
        refused = api.update_provider_interface_parameters(
            interface["id"], 0, 0, 0, concurrency=bad
        )
        assert "error" in refused
    loaded = Project.load(project.root).config.translator
    kept = next(item for item in loaded.interfaces if item.id == interface["id"])
    assert (kept.concurrency, kept.rpm) == (4, 10)


def test_provider_keys_are_shared_between_projects_with_the_same_endpoint(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    api = Api(app_state, event_bus, credential_store=store)
    endpoint = "https://models.example/v1"

    first_project = create_project(
        CreateProjectRequest(tmp_path / "first", "First", "ja", "en", "livemaker")
    )
    assert "error" not in api.open_project(str(first_project.root))
    first = api.save_provider_interface(
        {
            "name": "First project",
            "provider": "openai-compatible-api",
            "model": "model-a",
            "base_url": endpoint,
        },
        "shared-key",
    )["interface"]

    second_project = create_project(
        CreateProjectRequest(tmp_path / "second", "Second", "ja", "en", "livemaker")
    )
    assert "error" not in api.open_project(str(second_project.root))
    second = api.save_provider_interface(
        {
            "name": "Second project",
            "provider": "openai-compatible-api",
            "model": "model-b",
            "base_url": endpoint,
        },
    )["interface"]

    assert first["id"] != second["id"]
    assert api.provider_interface_keys(second["id"]) == {"api_keys": ["shared-key"]}


def test_clearing_shared_provider_keys_clears_other_projects(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    api = Api(app_state, event_bus, credential_store=store)
    endpoint = "https://models.example/v1"

    first_project = create_project(
        CreateProjectRequest(tmp_path / "first", "First", "ja", "en", "livemaker")
    )
    assert "error" not in api.open_project(str(first_project.root))
    first = api.save_provider_interface(
        {
            "name": "First project",
            "provider": "openai-compatible-api",
            "model": "model-a",
            "base_url": endpoint,
        },
        "shared-key",
    )["interface"]

    second_project = create_project(
        CreateProjectRequest(tmp_path / "second", "Second", "ja", "en", "livemaker")
    )
    assert "error" not in api.open_project(str(second_project.root))
    second = api.save_provider_interface(
        {
            "name": "Second project",
            "provider": "openai-compatible-api",
            "model": "model-b",
            "base_url": endpoint,
        },
    )["interface"]

    assert "error" not in api.clear_provider_interface_keys(second["id"])
    assert "error" not in api.open_project(str(first_project.root))
    assert api.provider_interface_keys(first["id"]) == {"api_keys": []}


def test_provider_keys_stay_separate_for_different_endpoints(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    api = Api(app_state, event_bus, credential_store=store)

    first_project = create_project(
        CreateProjectRequest(tmp_path / "first", "First", "ja", "en", "livemaker")
    )
    assert "error" not in api.open_project(str(first_project.root))
    api.save_provider_interface(
        {
            "name": "First project",
            "provider": "openai-compatible-api",
            "model": "model-a",
            "base_url": "https://first.example/v1",
        },
        "first-key",
    )

    second_project = create_project(
        CreateProjectRequest(tmp_path / "second", "Second", "ja", "en", "livemaker")
    )
    assert "error" not in api.open_project(str(second_project.root))
    second = api.save_provider_interface(
        {
            "name": "Second project",
            "provider": "openai-compatible-api",
            "model": "model-b",
            "base_url": "https://second.example/v1",
        },
    )["interface"]

    assert api.provider_interface_keys(second["id"]) == {"api_keys": []}


def test_reading_provider_keys_migrates_existing_interface_scoped_keys(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    api = Api(app_state, event_bus, credential_store=store)
    endpoint = "https://models.example/v1"
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "Migration", "ja", "en", "livemaker")
    )
    assert "error" not in api.open_project(str(project.root))
    interface = api.save_provider_interface(
        {
            "name": "Old storage",
            "provider": "openai-compatible-api",
            "model": "model-a",
            "base_url": endpoint,
        },
    )["interface"]
    old_scope = interface_credential_scope(interface["id"])
    store.add(interface["provider"], old_scope, "old-key")

    assert api.provider_interface_keys(interface["id"]) == {"api_keys": ["old-key"]}
    assert store.get_all(interface["provider"], endpoint) == ("old-key",)
    assert store.get_all(interface["provider"], old_scope) == ()


def test_provider_interface_keys_returns_only_the_selected_interfaces_keys(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    api = Api(app_state, event_bus, credential_store=store)
    _open_project(api, tmp_path)
    selected = api.save_provider_interface(
        {
            "name": "Selected",
            "provider": "openai-compatible-api",
            "model": "model-a",
            "base_url": "https://selected.example/v1",
        },
        "first-key\nsecond-key",
    )["interface"]
    api.save_provider_interface(
        {
            "name": "Other",
            "provider": "openai-compatible-api",
            "model": "model-b",
            "base_url": "https://other.example/v1",
        },
        "other-key",
    )

    assert api.provider_interface_keys(selected["id"]) == {
        "api_keys": ["first-key", "second-key"],
    }


def test_provider_model_fetch_and_test_use_the_interface_credential(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    calls = []

    def fetch_models(interface, secret):
        calls.append((interface.provider, interface.base_url, secret))
        return ("model-a", "model-b")

    api = Api(
        app_state,
        event_bus,
        credential_store=store,
        model_fetcher=fetch_models,
    )
    _open_project(api, tmp_path)
    saved = api.save_provider_interface(
        {
            "name": "Custom",
            "provider": "openai-compatible-api",
            "model": "model-a",
            "base_url": "https://models.example/v1",
        },
        "private-key",
    )
    interface_id = saved["interface"]["id"]

    models = api.discover_provider_models(
        "openai-compatible-api",
        "https://models.example/v1",
        None,
        None,
        interface_id,
    )
    tested = api.test_provider_interface(interface_id)

    assert models == {"models": ["model-a", "model-b"]}
    assert tested == {"ok": True, "models": 2}
    assert calls == [
        ("openai-compatible-api", "https://models.example/v1", "private-key"),
        ("openai-compatible-api", "https://models.example/v1", "private-key"),
    ]


def test_listing_models_at_an_edited_address_keeps_the_stored_key_home(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    calls = []

    def fetch_models(interface, secret):
        calls.append((interface.base_url, secret))
        return ("model-a",)

    api = Api(app_state, event_bus, credential_store=store, model_fetcher=fetch_models)
    _open_project(api, tmp_path)
    saved = api.save_provider_interface(
        {
            "name": "Custom",
            "provider": "openai-compatible-api",
            "model": "model-a",
            "base_url": "https://models.example/v1",
        },
        "private-key",
    )
    interface_id = saved["interface"]["id"]

    for base_url in ("https://elsewhere.example/v1", "https://MODELS.example/v1/"):
        api.discover_provider_models("openai-compatible-api", base_url, None, None, interface_id)

    assert calls == [
        ("https://elsewhere.example/v1", None),
        ("https://MODELS.example/v1/", "private-key"),
    ]


def test_a_stored_key_the_page_sends_back_stays_home_at_an_edited_address(
    app_state, event_bus, tmp_path: Path
) -> None:
    store = Base64CredentialStore(tmp_path / "credentials.json")
    calls = []

    def fetch_models(interface, secret):
        calls.append((interface.base_url, secret))
        return ("model-a",)

    api = Api(app_state, event_bus, credential_store=store, model_fetcher=fetch_models)
    _open_project(api, tmp_path)
    values = {
        "name": "Custom",
        "provider": "openai-compatible-api",
        "model": "model-a",
        "base_url": "https://models.example/v1",
    }
    interface_id = api.save_provider_interface(values, "private-key")["interface"]["id"]
    relay = {**values, "id": interface_id, "base_url": "https://relay.example/v1"}

    api.discover_provider_models(
        "openai-compatible-api", relay["base_url"], "private-key", None, interface_id
    )
    api.save_provider_interface(relay, "private-key\nrelay-key")

    assert calls == [("https://relay.example/v1", None)]
    assert api.provider_interface_keys(interface_id) == {"api_keys": ["relay-key"]}


def test_a_running_translation_shows_each_saved_file_before_it_ends(
    api: Api,
    tmp_path: Path,
    fake_window: FakeWindow,
    monkeypatch: pytest.MonkeyPatch,
    credential_store: Base64CredentialStore,
) -> None:
    project = _seed_project(
        api,
        tmp_path,
        "live",
        [
            StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="おはよう")]),
            StringFile(source_file="b.lsb", units=[TextUnit(id="u2", source="こんにちは")]),
        ],
    )
    translator = project.config.translator.model_copy(update={"batch_size": 1})
    project.save_config(project.config.model_copy(update={"translator": translator}))
    assert "error" not in api.open_project(str(project.root))
    second_asked = Event()
    release = Event()

    class _SecondWaits:
        name = "fake"

        def translate(self, batch, context, glossary=None):
            if batch[0].source == "こんにちは":
                second_asked.set()
                release.wait(5)
            return ["譯" for _ in batch]

    monkeypatch.setattr(registry_module, "default_credential_store", lambda: credential_store)
    monkeypatch.setattr(
        registry_module,
        "make_translation_lanes",
        lambda project, secrets=None: (TranslationLane(_SecondWaits()),),
    )

    assert "error" not in api.start_job("translate")
    assert second_asked.wait(5)
    try:
        rows = api.unit_rows()["rows"]
        assert {row["id"]: row["target"] for row in rows}["u1"] == "譯"
    finally:
        release.set()
        _wait_for_terminal(fake_window, "translate")
        api.jobs.join(timeout=5)


def test_bracket_fixes_are_counted_first_and_then_written(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    units = [TextUnit(id="u1", source="「やめて」", target="（住手）", status=Status.MT)]
    project = _seed_project(
        api, tmp_path, "brackets", [StringFile(source_file="a.lsb", units=units)]
    )
    assert "error" not in api.open_project(str(project.root))
    assert api.brackets_preview() == {"fixed": 1}

    assert "error" not in api.start_job("fix-brackets")

    event = _wait_for_terminal(fake_window, "fix-brackets")
    api.jobs.join(timeout=5)
    assert event["type"] == "succeeded"
    assert event["detail"] == {"fixed": 1}
    saved = Project.load(project.root).load_string_files()[0].units
    assert saved[0].target == "「住手」"
    assert api.brackets_preview() == {"fixed": 0}
