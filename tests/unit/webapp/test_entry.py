"""RED tests for ``python -m bokucho.webapp``: window config + close cleanup."""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from webview import util as webview_util

import bokucho.webapp.__main__ as entry
from bokucho.application.errors import ProviderFailure
from bokucho.infrastructure.credentials import Base64CredentialStore
from bokucho.user_settings import UserSettingsStore
from bokucho.webapp.api import Api
from bokucho.webapp.events import EventBus
from bokucho.webapp.state import AppState


class _RecordingClosedEvent(list):
    """Mimics pywebview's ``Event.__iadd__`` so registered callbacks are visible."""

    def __iadd__(self, callback):
        self.append(callback)
        return self


class _FakeEvents:
    def __init__(self) -> None:
        self.before_show = _RecordingClosedEvent()
        self.closing = _RecordingClosedEvent()
        self.closed = _RecordingClosedEvent()
        self.maximized = _RecordingClosedEvent()
        self.restored = _RecordingClosedEvent()


class _FakeWindow:
    def __init__(self) -> None:
        self.events = _FakeEvents()
        self.file_dialog_calls: list[tuple[object, dict]] = []
        self.file_dialog_result: tuple[str, ...] | None = None
        self.exposed: dict[str, object] = {}

    def create_file_dialog(self, dialog_type, **kwargs):
        self.file_dialog_calls.append((dialog_type, kwargs))
        return self.file_dialog_result

    def expose(self, *functions):
        self.exposed.update({function.__name__: function for function in functions})


class _StubJob:
    def __init__(self) -> None:
        self.cancel_called = False
        self.join_timeout: float | None = None

    def cancel(self) -> None:
        self.cancel_called = True

    def join(self, timeout: float | None = None) -> None:
        self.join_timeout = timeout


class _StubWatcher:
    def __init__(self) -> None:
        self.stop_called = False

    def stop(self) -> None:
        self.stop_called = True


def test_main_configures_window_and_internal_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_window = _FakeWindow()
    create_calls: dict = {}
    start_calls: dict = {}

    def fake_create_window(title, url=None, js_api=None, **kwargs):
        create_calls["title"] = title
        create_calls["url"] = url
        create_calls["js_api"] = js_api
        return fake_window

    def fake_start(*args, **kwargs):
        start_calls.update(kwargs)

    monkeypatch.setattr(entry.webview, "create_window", fake_create_window)
    monkeypatch.setattr(entry.webview, "start", fake_start)

    entry.main()

    assert create_calls["url"].replace("\\", "/").endswith("web/index.html")
    assert Path(create_calls["url"]).is_file()
    assert create_calls["js_api"] is None
    assert "open_project" in fake_window.exposed
    assert start_calls.get("http_server") is True


class _Page:
    """What pywebview's own injection and dispatch need from a window."""

    uid = "page"
    js_api_endpoint = None
    text_select = False
    zoomable = False
    draggable = False
    easy_drag = False
    frameless = True
    state: dict = {}

    def __init__(self, js_api, functions: dict) -> None:
        self._js_api = js_api
        self._functions = functions
        self._expose_lock = threading.Lock()
        self.events = SimpleNamespace(
            before_load=threading.Event(),
            loaded=threading.Event(),
            _pywebviewready=threading.Event(),
        )
        self.scripts: list[str] = []

    def run_js(self, code: str) -> None:
        self.scripts.append(code)

    evaluate_js = run_js


class _Inline:
    def __init__(self, target, args=(), **kwargs) -> None:
        self._run = lambda: target(*args)

    def start(self) -> None:
        self._run()


def _launched_page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Page:
    fake_window = _FakeWindow()
    create_calls: dict = {}

    def fake_create_window(title, url=None, js_api=None, **kwargs):
        create_calls["js_api"] = js_api
        return fake_window

    monkeypatch.setattr(entry.webview, "create_window", fake_create_window)
    monkeypatch.setattr(entry.webview, "start", lambda *a, **k: None)
    store = Base64CredentialStore(tmp_path / "credentials.json")
    store.add("openai", None, "sk-the-page-must-not-see")
    state = AppState(settings=UserSettingsStore(tmp_path / "settings.json"))
    events = EventBus()
    entry.main(state=state, events=events, api=Api(state, events, credential_store=store))
    return _Page(create_calls["js_api"], fake_window.exposed)


def test_the_page_is_offered_only_the_bridge_methods(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = _launched_page(monkeypatch, tmp_path)

    webview_util.inject_pywebview("edgechromium", page)
    assert page.events.loaded.wait(5)

    listed = re.search(r'\[\{"func".*?\}\]', page.scripts[-1], re.S)
    offered = {item["func"] for item in json.loads(listed.group(0))}
    assert {name for name in vars(Api) if not name.startswith("_")} <= offered
    assert not any("." in name for name in offered)


def test_the_page_cannot_walk_from_a_bridge_method_to_what_it_holds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = _launched_page(monkeypatch, tmp_path)
    monkeypatch.setattr(webview_util, "Thread", _Inline)

    webview_util.js_bridge_call(page, "recent_projects", [], "1")
    webview_util.js_bridge_call(
        page, "open_project.__self__.credential_store.get_all", ["openai", None], "2"
    )

    assert len(page.scripts) == 1
    assert "sk-the-page-must-not-see" not in page.scripts[0]


def test_main_binds_native_folder_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_window = _FakeWindow()
    fake_window.file_dialog_result = ("C:/games/sample",)
    create_calls: dict = {}

    def fake_create_window(title, url=None, js_api=None, **kwargs):
        create_calls["js_api"] = js_api
        return fake_window

    monkeypatch.setattr(entry.webview, "create_window", fake_create_window)
    monkeypatch.setattr(entry.webview, "start", lambda *a, **k: None)

    entry.main()

    result = fake_window.exposed["choose_directory"]("C:/games")
    assert result == "C:/games/sample"
    assert fake_window.file_dialog_calls == [
        (entry.webview.FileDialog.FOLDER, {"directory": "C:/games"})
    ]


def test_window_closed_callback_cancels_job_and_stops_watcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_window = _FakeWindow()
    monkeypatch.setattr(entry.webview, "create_window", lambda *a, **k: fake_window)
    monkeypatch.setattr(entry.webview, "start", lambda *a, **k: None)

    stub_job = _StubJob()
    stub_watcher = _StubWatcher()
    state = AppState(settings=UserSettingsStore(tmp_path / "settings.json"))
    state.watcher = stub_watcher

    entry.main(state=state, jobs=stub_job)

    assert len(fake_window.events.closed) == 1
    fake_window.events.closed[0]()

    assert stub_job.cancel_called is True
    assert stub_job.join_timeout is not None
    assert stub_watcher.stop_called is True


def test_a_warning_the_locale_cannot_encode_is_logged_instead_of_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp950")
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp950"))
    monkeypatch.setattr(entry.webview, "create_window", lambda *a, **k: _FakeWindow())
    monkeypatch.setattr(entry.webview, "start", lambda *a, **k: None)
    entry.main()

    line = "メニューを開いてください。"
    try:
        raise ProviderFailure(f"OpenAI-compatible provider returned an unusable response: {line}")
    except ProviderFailure as failure:
        logging.getLogger("bokucho.application.translation_limits").warning(
            "Task failed (attempt 1 of 3), asking again: %s", failure
        )

    stderr.flush()
    assert line in stderr.buffer.getvalue().decode("utf-8")


def test_the_page_script_is_served_as_javascript_whatever_the_registry_says() -> None:
    """A registry that calls .js text/plain would leave the window blank."""
    import subprocess
    import sys

    probe = (
        "import mimetypes; mimetypes.add_type('text/plain', '.js'); "
        "import bokucho.webapp.__main__; print(mimetypes.guess_type('app.js')[0])"
    )
    served = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert served == "text/javascript"


class _DisposableWindow(_FakeWindow):
    """A window whose page cannot be scripted once it has closed."""

    def __init__(self) -> None:
        super().__init__()
        self.disposed = False
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> None:
        if self.disposed:
            raise RuntimeError("Cannot access a disposed object: WebView2")
        self.scripts.append(script)


class _JobThatReportsItsCancellation(_StubJob):
    """Like the real runner: a cancelled job says so on the event bus."""

    def __init__(self, events) -> None:
        super().__init__()
        self._events = events

    def cancel(self) -> None:
        super().cancel()
        self._events.push({"type": "cancelled", "job": "translate"})


def test_nothing_is_sent_to_the_page_after_the_window_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bokucho.webapp.events import EventBus

    window = _DisposableWindow()
    monkeypatch.setattr(entry.webview, "create_window", lambda *a, **k: window)
    monkeypatch.setattr(entry.webview, "start", lambda *a, **k: None)
    events = EventBus()
    job = _JobThatReportsItsCancellation(events)
    state = AppState(settings=UserSettingsStore(tmp_path / "settings.json"))

    entry.main(state=state, events=events, jobs=job)
    for callback in window.events.closing:
        callback()
    window.disposed = True
    for callback in window.events.closed:
        callback()
    events.push({"type": "log", "job": "translate", "message": "late"})

    assert job.cancel_called is True
    assert window.scripts == []
