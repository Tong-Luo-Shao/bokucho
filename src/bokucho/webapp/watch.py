"""Watch project files and protect active web edits from external changes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from bokucho.application.projects import string_filename
from bokucho.model import StringFile
from bokucho.sync.atomic import OverwriteRefused, file_lock, observe_overwrites, written_here
from bokucho.sync.json_io import load_string_file
from bokucho.webapp.events import EventBus
from bokucho.webapp.state import AppState


class ProjectWatcher:
    """Watches an open project's ``strings/`` directory for external edits."""

    def __init__(self, state: AppState, events: EventBus) -> None:
        self._state = state
        self._events = events
        self._observer: Observer | None = None
        self._stop_observing_overwrites: Callable[[], None] | None = None

    def start(self) -> None:
        project = self._state.project
        if project is None or self._observer is not None:
            return
        project.strings_dir.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(_Handler(self), str(project.strings_dir), recursive=False)
        observer.start()
        self._observer = observer
        self._stop_observing_overwrites = observe_overwrites(self._before_overwrite)

    def stop(self) -> None:
        if self._stop_observing_overwrites is not None:
            self._stop_observing_overwrites()
            self._stop_observing_overwrites = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    def _on_changed(self, path: str) -> None:
        changed_path = Path(path)
        if self._holds_strings(changed_path):
            with file_lock(changed_path):
                self._check(changed_path)

    def _holds_strings(self, path: Path) -> bool:
        project = self._state.project
        return (
            project is not None
            and path.suffix == ".json"
            and path.parent.resolve(strict=False) == project.strings_dir.resolve(strict=False)
        )

    def _before_overwrite(self, path: Path, text: str) -> None:
        self._check(path)
        conflict = self._state.pending_conflict
        project = self._state.project
        if conflict is None or project is None:
            return
        conflicted = project.strings_dir / string_filename(conflict["source_file"])
        if path.resolve(strict=False) != conflicted.resolve(strict=False):
            return
        try:
            unit = StringFile.model_validate_json(text).by_id(conflict["unit_id"])
        except ValueError:
            return
        if (unit.target if unit is not None else "") != conflict["disk_target"]:
            raise OverwriteRefused(f"{path.name} changed outside Bokucho while it was being saved.")

    def _check(self, changed_path: Path) -> None:
        project = self._state.project
        if project is None or not self._holds_strings(changed_path):
            return
        if written_here(changed_path):
            return
        try:
            string_file = load_string_file(changed_path)
        except (OSError, ValueError):
            return
        source_file = string_file.source_file
        expected = project.strings_dir / string_filename(source_file)
        if changed_path.resolve(strict=False) != expected.resolve(strict=False):
            return
        self._state.replace_file(string_file)
        self._events.push({"type": "strings-changed", "file": source_file})
        edit = self._state.editing
        if edit is None or edit[0] != source_file:
            return
        _, unit_id, draft_target, baseline = edit
        unit = string_file.by_id(unit_id)
        disk_target = unit.target if unit is not None else ""
        if disk_target == baseline:
            return
        self._state.pending_conflict = {
            "source_file": source_file,
            "unit_id": unit_id,
            "disk_target": disk_target,
            "draft_target": draft_target,
        }
        self._events.push({"type": "conflict", "file": source_file, "unit_id": unit_id})


class _Handler(FileSystemEventHandler):
    """Routes filesystem events for the watched directory to the watcher."""

    def __init__(self, watcher: ProjectWatcher) -> None:
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
