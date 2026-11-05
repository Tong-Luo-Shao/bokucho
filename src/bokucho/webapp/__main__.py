"""Entry point: ``python -m bokucho.webapp`` -- opens the pywebview window."""

from __future__ import annotations

import logging
import mimetypes
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import webview

from bokucho import logs
from bokucho.appdirs import app_data_dir
from bokucho.webapp.api import Api
from bokucho.webapp.dev_reload import DevWatcher, dev_mode_enabled, relaunch
from bokucho.webapp.events import EventBus
from bokucho.webapp.jobs import JobRunner, JobState
from bokucho.webapp.native_window import (
    begin_native_window_drag,
    begin_native_window_resize,
    enable_borderless_resize,
    enable_rounded_corners,
    fit_maximized_to_work_area,
    set_frameless_resize,
    set_native_window_size,
)
from bokucho.webapp.state import AppState

mimetypes.add_type("text/javascript", ".js")

TITLE = "Bokucho"
INDEX_HTML = Path(__file__).resolve().parent.parent / "web" / "index.html"
DEFAULT_WINDOW_WIDTH = 1283
DEFAULT_WINDOW_HEIGHT = 765
GAME_FILE_TYPES = ("Game executable (*.exe;*.dat)", "All files (*.*)")
DLL_FILE_TYPES = ("UIF DLL (*.dll)", "All files (*.*)")
FILE_TYPES = {"game": GAME_FILE_TYPES, "dll": DLL_FILE_TYPES}


def page_api(api: Api) -> tuple[Callable[..., Any], ...]:
    """``api``'s own methods, which pywebview dispatches by exact name alone."""
    return tuple(getattr(api, name) for name in vars(Api) if not name.startswith("_"))


class _Shell:
    """The frameless window and the native controls the page draws for it."""

    def __init__(self, state: AppState, events: EventBus, jobs: JobRunner) -> None:
        self.state = state
        self.events = events
        self.jobs = jobs
        self.window: Any = None
        self.watcher: DevWatcher | None = None
        self.maximized = False

    def pick_folder(self, current_path: str) -> str | None:
        selected = self.window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=current_path,
        )
        return selected[0] if selected else None

    def pick_file(self, current_path: str, kind: str = "game") -> str | None:
        start = Path(current_path)
        selected = self.window.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=str(start.parent) if start.is_file() else current_path,
            file_types=FILE_TYPES[kind],
        )
        return selected[0] if selected else None

    @staticmethod
    def open_folder(path: Path) -> None:
        os.startfile(path)  # type: ignore[attr-defined]

    def minimize_window(self) -> dict[str, bool]:
        self.window.minimize()
        return {"minimized": True}

    def toggle_maximize_window(self) -> dict[str, bool]:
        if self.maximized:
            self.window.restore()
            self._on_restored()
        else:
            set_frameless_resize(self.window, False)
            self.window.maximize()
            fit_maximized_to_work_area(self.window)
            self.maximized = True
        return {"maximized": self.maximized}

    def begin_window_drag(self) -> dict[str, bool]:
        self.maximized = begin_native_window_drag(self.window)
        set_frameless_resize(self.window, not self.maximized)
        return {"maximized": self.maximized}

    def begin_window_resize(self, edge: str) -> dict[str, bool]:
        return {"resizing": begin_native_window_resize(self.window, edge)}

    def close_window(self) -> dict[str, bool]:
        self.window.destroy()
        return {"closed": True}

    def open(self, api: Api) -> None:
        self.window = webview.create_window(
            TITLE,
            url=str(INDEX_HTML),
            frameless=True,
            easy_drag=False,
            shadow=False,
            resizable=True,
            width=DEFAULT_WINDOW_WIDTH,
            height=DEFAULT_WINDOW_HEIGHT,
            min_size=(640, 420),
        )
        self.window.expose(
            *page_api(api),
            self.minimize_window,
            self.toggle_maximize_window,
            self.begin_window_drag,
            self.begin_window_resize,
            self.close_window,
        )
        self.events.bind(self.window)
        dev_mode = dev_mode_enabled()
        if dev_mode:
            self.watcher = DevWatcher(self.window, lambda: self.jobs.state is not JobState.IDLE)
            self.watcher.start()
        self.window.events.before_show += self._on_before_show
        self.window.events.maximized += self._on_maximized
        self.window.events.restored += self._on_restored
        self.window.events.closing += self.events.close
        self.window.events.closed += self._on_closed
        webview.start(http_server=True, debug=dev_mode)
        if self.watcher is not None and self.watcher.restart_requested:
            relaunch()

    def _on_before_show(self) -> None:
        enable_borderless_resize(self.window)
        set_native_window_size(self.window, DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT)
        enable_rounded_corners(self.window)

    def _on_maximized(self) -> None:
        set_frameless_resize(self.window, False)
        fit_maximized_to_work_area(self.window)
        self.maximized = True

    def _on_restored(self) -> None:
        set_frameless_resize(self.window, True)
        self.maximized = False

    def _on_closed(self) -> None:
        self.events.close()
        self.jobs.cancel()
        self.jobs.join(timeout=5)
        if self.state.watcher is not None:
            self.state.watcher.stop()
        if self.watcher is not None:
            self.watcher.stop()


def main(
    *,
    state: AppState | None = None,
    events: EventBus | None = None,
    jobs: JobRunner | None = None,
    api: Api | None = None,
) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logs.configure(logging.INFO, path=app_data_dir() / "bokucho.log")
    state = state if state is not None else AppState()
    events = events if events is not None else EventBus()
    jobs = jobs if jobs is not None else JobRunner(events)
    shell = _Shell(state, events, jobs)
    api = api if api is not None else Api(
        state,
        events,
        job_runner=jobs,
        folder_picker=shell.pick_folder,
        file_picker=shell.pick_file,
        folder_opener=shell.open_folder,
    )
    shell.open(api)


if __name__ == "__main__":
    main()
