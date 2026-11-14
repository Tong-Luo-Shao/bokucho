"""Tests for the engine registry lookup and the adapter factory."""

import pytest

from bokucho.engines import get_adapter
from bokucho.infrastructure import factories
from bokucho.user_settings import GuiSettings, UserSettingsStore


def test_get_adapter_unknown_raises():
    with pytest.raises(ValueError, match="Unknown engine adapter"):
        get_adapter("nope")


def test_engine_factory_passes_livemaker_proxy_map(new_project, monkeypatch):
    project = new_project("t")
    adapter = object()
    captured = {}

    def get_fake_adapter(name, **kwargs):
        captured.update(name=name, **kwargs)
        return adapter

    monkeypatch.setattr(factories, "get_adapter", get_fake_adapter)

    assert factories.make_engine_adapter(project) is adapter
    assert captured == {
        "name": "livemaker",
        "proxy_map": project.root / project.config.engine.proxy_map,
        "workspace": project.root / "unpacked",
    }


def test_a_renpy_project_without_its_own_sdk_uses_the_apps(new_project, tmp_path):
    UserSettingsStore().save(GuiSettings(renpy_sdk_path=(tmp_path / "app-sdk").as_posix()))
    adapter = factories.make_engine_adapter(new_project(engine="renpy"))
    assert adapter.sdk_path == tmp_path / "app-sdk"


def test_a_renpy_projects_own_sdk_wins_over_the_apps(new_project, tmp_path):
    UserSettingsStore().save(GuiSettings(renpy_sdk_path=(tmp_path / "app-sdk").as_posix()))
    project = new_project(engine="renpy")
    engine = project.config.engine.model_copy(update={"sdk_path": "own-sdk"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    adapter = factories.make_engine_adapter(project.load(project.root))
    assert adapter.sdk_path == project.root / "own-sdk"
