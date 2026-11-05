"""Shared fixtures for webapp bridge tests: Api, AppState, and a fake window."""

from __future__ import annotations

from pathlib import Path

import pytest

from bokucho.user_settings import UserSettingsStore
from bokucho.webapp.api import Api
from bokucho.webapp.events import EventBus
from bokucho.webapp.state import AppState


class FakeWindow:
    """Records `evaluate_js` calls instead of driving a real webview."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def evaluate_js(self, script: str) -> None:
        self.calls.append(script)


@pytest.fixture
def fake_window() -> FakeWindow:
    return FakeWindow()


@pytest.fixture
def app_state(tmp_path: Path) -> AppState:
    return AppState(settings=UserSettingsStore(tmp_path / "settings.json"))


@pytest.fixture(autouse=True)
def _stop_leaked_watcher(app_state: AppState):
    """Stop any watchdog Observer ``open_project`` started during the test."""
    yield
    if app_state.watcher is not None:
        app_state.watcher.stop()


@pytest.fixture
def event_bus(fake_window: FakeWindow) -> EventBus:
    return EventBus(fake_window)


@pytest.fixture
def registry() -> dict:
    """Fresh per-test job registry -- tests inject fake operations directly."""
    return {}


@pytest.fixture
def api(app_state: AppState, event_bus: EventBus, registry: dict) -> Api:
    return Api(app_state, event_bus, registry=registry)
