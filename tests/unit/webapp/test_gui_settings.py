from __future__ import annotations

import pytest
from pydantic import ValidationError

from bokucho.webapp.api import Api
from bokucho.webapp.state import AppState


def test_older_gui_settings_receive_font_defaults(api: Api, app_state: AppState) -> None:
    app_state.settings.path.write_text('{"locale": "en", "theme": "light"}', encoding="utf-8")

    settings = api.gui_settings()

    assert settings["font_size"] == 16
    assert settings["font_family"] == ""
    assert settings["locale"] == "en"
    assert settings["theme"] == "light"
    assert list(app_state.settings.path.parent.glob("settings.json.invalid*")) == []


def test_gui_font_preferences_survive_reload_and_older_save_calls(
    api: Api, app_state: AppState,
) -> None:
    api.save_gui_settings(font_family="Microsoft JhengHei UI", font_size=18)
    api.save_gui_settings("light", "ice", "roomy", "en", True)

    reloaded = app_state.settings.load()
    assert reloaded.font_family == "Microsoft JhengHei UI"
    assert reloaded.font_size == 18
    assert reloaded.theme == "light"
    assert reloaded.nav_expanded is True
    assert api.gui_settings()["font_size"] == 18


def test_clay_is_the_default_accent_and_older_accents_still_load(
    api: Api, app_state: AppState,
) -> None:
    assert api.gui_settings()["accent"] == "clay"

    app_state.settings.path.write_text('{"accent": "lime"}', encoding="utf-8")
    assert api.gui_settings()["accent"] == "lime"
    assert list(app_state.settings.path.parent.glob("settings.json.invalid*")) == []

    api.save_gui_settings(accent="clay")
    assert app_state.settings.load().accent == "clay"


@pytest.mark.parametrize("patch", [
    {"font_size": 11},
    {"font_size": 25},
    {"font_family": "x" * 129},
    {"font_family": "font\nname"},
])
def test_invalid_gui_font_preferences_preserve_saved_settings(
    api: Api, app_state: AppState, patch: dict[str, str | int],
) -> None:
    api.save_gui_settings(font_family="Segoe UI", font_size=20)
    original = app_state.settings.path.read_bytes()

    with pytest.raises(ValidationError):
        api.save_gui_settings(**patch)

    assert app_state.settings.path.read_bytes() == original
