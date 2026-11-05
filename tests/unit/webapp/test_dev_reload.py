"""Tests for the dev-mode switch and the asset reloader's file filter."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bokucho.webapp.dev_reload import DEV_ENV, DevWatcher, dev_mode_enabled


class _FakeWindow:
    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.destroyed = False

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)

    def destroy(self) -> None:
        self.destroyed = True


@pytest.mark.parametrize("value", ["1", "true", "on", "yes"])
def test_dev_mode_is_on_for_a_truthy_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(DEV_ENV, value)

    assert dev_mode_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
def test_dev_mode_is_off_for_an_explicit_no(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(DEV_ENV, value)

    assert dev_mode_enabled() is False


def test_a_file_neither_side_loads_is_ignored(tmp_path: Path) -> None:
    window = _FakeWindow()
    watcher = DevWatcher(window, lambda: False, root=tmp_path, debounce=0.05)
    watcher.start()
    try:
        (tmp_path / "theme.css.swp").write_text("scratch", encoding="utf-8")
        (tmp_path / "api.pyc").write_bytes(b"\x00")
        time.sleep(1.0)
    finally:
        watcher.stop()

    assert window.scripts == []
    assert window.destroyed is False
    assert watcher.restart_requested is False


@pytest.mark.parametrize(
    "order", [("api.py", "theme.css"), ("theme.css", "api.py")]
)
def test_a_module_in_the_burst_outranks_an_asset(
    tmp_path: Path, order: tuple[str, str]
) -> None:
    window = _FakeWindow()
    watcher = DevWatcher(window, lambda: False, root=tmp_path, debounce=0.05)

    for name in order:
        watcher._on_changed(str(tmp_path / name))
    time.sleep(0.3)

    assert watcher.restart_requested is True
    assert window.scripts == []
