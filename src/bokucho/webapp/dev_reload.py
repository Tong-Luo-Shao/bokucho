"""Developer mode: the web inspector, and picking edits up without a relaunch."""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

DEV_ENV = "BOKUCHO_DEV"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
RELOAD_DEBOUNCE = 0.25
RELOAD_SUFFIXES = frozenset({".css", ".html", ".js"})
RESTART_SUFFIX = ".py"
_DISABLED = frozenset({"", "0", "false", "no", "off"})


def dev_mode_enabled() -> bool:
    return os.environ.get(DEV_ENV, "").strip().lower() not in _DISABLED


def relaunch() -> None:
    """Replace this process with the command that started it."""
    os.execv(sys.orig_argv[0], sys.orig_argv)


class DevWatcher:
    """Watches the package: a web asset reloads the window, a module restarts it."""

    def __init__(
        self,
        window,
        busy: Callable[[], bool],
        root: Path = PACKAGE_ROOT,
        debounce: float = RELOAD_DEBOUNCE,
    ) -> None:
        self._window = window
        self._busy = busy
        self._root = root
        self._debounce = debounce
        self._observer: Observer | None = None
        self._timer: threading.Timer | None = None
        self._restart_pending = False
        self.restart_requested = False

    def start(self) -> None:
        if self._observer is not None:
            return
        observer = Observer()
        observer.schedule(_Handler(self), str(self._root), recursive=True)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    def _on_changed(self, path: str) -> None:
        suffix = Path(path).suffix.lower()
        if suffix == RESTART_SUFFIX:
            self._restart_pending = True
        elif suffix not in RELOAD_SUFFIXES:
            return
        self._schedule()

    def _schedule(self) -> None:
        """Restart the debounce, keeping what the burst has asked for so far."""
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce, self._settle)
        self._timer.start()

    def _settle(self) -> None:
        if self._restart_pending:
            self._restart_pending = False
            self._restart()
            return
        self._reload()

    def _reload(self) -> None:
        self._window.evaluate_js("window.location.reload()")

    def _restart(self) -> None:
        if self._busy():
            logger.warning("Not restarting for a code change: a job is still running")
            return
        self.restart_requested = True
        self._window.destroy()


class _Handler(FileSystemEventHandler):
    """Routes filesystem events for the watched tree to the watcher."""

    def __init__(self, watcher: DevWatcher) -> None:
        self._watcher = watcher

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._watcher._on_changed(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._watcher._on_changed(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._watcher._on_changed(event.dest_path)
