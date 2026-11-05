"""RED/GREEN tests for the watchdog file watcher and conflict resolution."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import bokucho.application.text as text_module
import bokucho.sync.atomic as atomic
from bokucho.application.projects import string_filename
from bokucho.model import StringFile, TextUnit
from bokucho.project import Project
from bokucho.sync.json_io import dump_string_file, load_string_file
from bokucho.webapp import api as api_module
from bokucho.webapp import state as state_module
from bokucho.webapp.api import Api
from bokucho.webapp.watch import ProjectWatcher

from .conftest import FakeWindow

_PREFIX = "bokucho.onEvent("


def _seed_project(api: Api, tmp_path: Path, name: str, target: str = "") -> Project:
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
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[TextUnit(id="u1", source="hello", target=target)],
        ),
        string_filename("a.lsb"),
    )
    return Project.load(project.root)


def _write_from_another_program(path: Path, target: str) -> None:
    string_file = StringFile(
        source_file="a.lsb", units=[TextUnit(id="u1", source="hello", target=target)]
    )
    temporary = path.with_name(path.name + ".other")
    temporary.write_text(dump_string_file(string_file), encoding="utf-8")
    atomic._replace_with_retry(temporary, path)


def _watcher(api: Api) -> ProjectWatcher:
    watcher = api.state.watcher
    assert isinstance(watcher, ProjectWatcher)
    return watcher


def _stop_file_events(api: Api) -> None:
    observer = _watcher(api)._observer
    assert observer is not None
    observer.stop()
    observer.join(timeout=2)


def _write_from_bokucho(project: Project) -> None:
    path = project.strings_dir / string_filename("a.lsb")
    project.save_string_file(load_string_file(path), string_filename("a.lsb"))


def _pushed_events(fake_window: FakeWindow) -> list[dict]:
    import json

    return [json.loads(call[len(_PREFIX) : -1]) for call in fake_window.calls]


def _conflict_events(fake_window: FakeWindow) -> list[dict]:
    return [event for event in _pushed_events(fake_window) if event["type"] == "conflict"]


def _wait_for_conflict(fake_window: FakeWindow, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conflicts = _conflict_events(fake_window)
        if conflicts:
            return conflicts[-1]
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for a conflict event")


def _assert_no_conflict_within(fake_window: FakeWindow, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert not _conflict_events(fake_window), "Unexpected conflict event"
        time.sleep(0.05)


def test_bridge_save_does_not_raise_a_conflict_for_its_own_write(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "own-write")
    assert "error" not in api.open_project(str(project.root))

    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")

    result = api.save_draft("a.lsb", "u1", "saved value")
    assert "error" not in result

    _assert_no_conflict_within(fake_window)


def test_a_later_write_after_a_saved_draft_is_not_a_conflict(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "saved-then-marked", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "saved value")
    assert "error" not in api.save_draft("a.lsb", "u1", "saved value")

    assert "error" not in api.set_unit_status("a.lsb", "u1", "mt")

    _assert_no_conflict_within(fake_window)


def test_a_bokucho_write_to_the_unit_being_edited_is_not_a_conflict(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "run-write", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")

    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[TextUnit(id="u1", source="hello", target="machine translation")],
        ),
        string_filename("a.lsb"),
    )

    _assert_no_conflict_within(fake_window)


def test_external_edit_while_a_unit_is_being_edited_pushes_a_conflict(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "external-edit", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")

    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, "changed by another program")

    event = _wait_for_conflict(fake_window)
    assert event["file"] == "a.lsb"
    assert event["unit_id"] == "u1"


def test_external_edit_right_after_a_save_still_pushes_a_conflict(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "edit-after-save", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.save_draft("a.lsb", "u1", "saved value")
    assert "error" not in api.begin_edit("a.lsb", "u1", "next draft")

    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, "changed by another program")

    _wait_for_conflict(fake_window)


def test_an_external_edit_bokucho_writes_over_unseen_is_still_a_conflict(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "unseen-edit", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")
    _stop_file_events(api)

    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, "changed by another program")
    _write_from_bokucho(project)

    _wait_for_conflict(fake_window)


def test_an_external_edit_from_before_the_edit_began_is_what_the_edit_starts_from(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "edit-before-editing", target="orig")
    assert "error" not in api.open_project(str(project.root))
    _stop_file_events(api)
    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, "changed by another program")
    _watcher(api)._on_changed(str(path))
    assert api.unit_detail("a.lsb", "u1")["target"] == "changed by another program"

    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")
    _write_from_bokucho(project)

    _assert_no_conflict_within(fake_window, seconds=1.0)


def test_an_edit_begun_from_text_the_editor_had_not_refreshed_conflicts_once_bokucho_writes(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "stale-editor", target="orig")
    assert "error" not in api.open_project(str(project.root))
    _stop_file_events(api)
    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, "changed by another program")
    _watcher(api)._on_changed(str(path))

    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft", "orig")
    _write_from_bokucho(project)

    _wait_for_conflict(fake_window)
    assert "error" in api.save_draft("a.lsb", "u1", "frontend draft", "orig")
    assert load_string_file(path).by_id("u1").target == "changed by another program"


def test_an_outside_write_landing_while_a_save_is_written_is_kept(
    api: Api, tmp_path: Path, fake_window: FakeWindow, monkeypatch
) -> None:
    project = _seed_project(api, tmp_path, "mid-save", target="orig")
    assert "error" not in api.open_project(str(project.root))
    _stop_file_events(api)
    path = project.strings_dir / string_filename("a.lsb")
    assert "error" not in api.begin_edit("a.lsb", "u1", "mine", "orig")
    read = text_module._find_unit

    def written_after_bokucho_read_it(*args):
        found = read(*args)
        _write_from_another_program(path, "outside")
        return found

    with monkeypatch.context() as patch:
        patch.setattr(text_module, "_find_unit", written_after_bokucho_read_it)
        assert "error" in api.save_draft("a.lsb", "u1", "mine", "orig")

    assert load_string_file(path).by_id("u1").target == "outside"
    assert "error" not in api.accept_disk()
    assert load_string_file(path).by_id("u1").target == "outside"


def test_a_half_written_file_is_skipped_until_it_is_whole(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "half-written", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")
    _stop_file_events(api)
    path = project.strings_dir / string_filename("a.lsb")

    path.write_text('{"source_file": "a.lsb", "units": [', encoding="utf-8")
    _watcher(api)._on_changed(str(path))
    assert not _conflict_events(fake_window)

    _write_from_another_program(path, "changed by another program")
    _watcher(api)._on_changed(str(path))
    _wait_for_conflict(fake_window)


def test_a_save_landing_while_a_run_reads_the_file_back_is_kept(
    api: Api, tmp_path: Path, monkeypatch
) -> None:
    project = _seed_project(api, tmp_path, "refresh-race")
    assert "error" not in api.open_project(str(project.root))
    saver = threading.Thread(target=api.save_draft, args=("a.lsb", "u1", "typed"))
    read_back = state_module.load_string_file

    def read_while_a_save_lands(path):
        string_file = read_back(path)
        saver.start()
        saver.join(timeout=0.5)
        return string_file

    monkeypatch.setattr(state_module, "load_string_file", read_while_a_save_lands)
    api.state.refresh_file("a.lsb")
    saver.join()

    assert api.unit_detail("a.lsb", "u1")["target"] == "typed"


def test_accept_disk_reloads_the_unit_from_disk(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "accept-disk", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")

    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, "disk value")
    _wait_for_conflict(fake_window)

    result = api.accept_disk()
    assert "error" not in result

    detail = api.unit_detail("a.lsb", "u1")
    assert "error" not in detail
    assert detail["target"] == "disk value"
    assert api.state.pending_conflict is None


def test_keep_draft_overrides_the_disk_version_and_clears_the_conflict(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "keep-draft", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "草稿")

    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, "changed by another program")
    _wait_for_conflict(fake_window)

    result = api.keep_draft()
    assert "error" not in result

    on_disk = load_string_file(path).by_id("u1")
    assert on_disk.target == "草稿"
    assert api.state.pending_conflict is None


def _change_on_disk(project: Project, target: str) -> Path:
    path = project.strings_dir / string_filename("a.lsb")
    _write_from_another_program(path, target)
    return path


def _changed_files(fake_window: FakeWindow) -> list[str]:
    return [
        event["file"]
        for event in _pushed_events(fake_window)
        if event["type"] == "strings-changed"
    ]


def test_saving_over_an_outside_edit_leaves_it_for_the_dialog(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "save-over-outside", target="orig")
    assert "error" not in api.open_project(str(project.root))
    _stop_file_events(api)
    path = _change_on_disk(project, "outside")

    assert "error" in api.save_draft("a.lsb", "u1", "mine", "orig")
    assert _conflict_events(fake_window)[-1]["unit_id"] == "u1"
    assert load_string_file(path).by_id("u1").target == "outside"

    assert "error" not in api.accept_disk()
    assert load_string_file(path).by_id("u1").target == "outside"


def test_keeping_the_editor_value_after_a_refused_save_writes_it(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "keep-refused", target="orig")
    assert "error" not in api.open_project(str(project.root))
    _stop_file_events(api)
    path = _change_on_disk(project, "outside")
    assert "error" in api.save_draft("a.lsb", "u1", "mine", "orig")

    assert "error" not in api.keep_draft()

    assert load_string_file(path).by_id("u1").target == "mine"


def test_saving_over_a_change_bokucho_made_is_not_refused(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "save-over-run", target="orig")
    assert "error" not in api.open_project(str(project.root))
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[TextUnit(id="u1", source="hello", target="machine translation")],
        ),
        string_filename("a.lsb"),
    )

    assert "error" not in api.save_draft("a.lsb", "u1", "mine", "orig")

    path = project.strings_dir / string_filename("a.lsb")
    assert load_string_file(path).by_id("u1").target == "mine"
    assert not _conflict_events(fake_window)


def test_an_outside_edit_while_nothing_is_edited_is_shown(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "outside-shown", target="orig")
    assert "error" not in api.open_project(str(project.root))

    _change_on_disk(project, "outside")

    deadline = time.monotonic() + 10
    while not _changed_files(fake_window) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _changed_files(fake_window)[-1] == "a.lsb"
    assert api.unit_detail("a.lsb", "u1")["target"] == "outside"
    assert not _conflict_events(fake_window)


def test_a_save_from_bokucho_is_not_announced_as_a_change(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "own-save-quiet", target="orig")
    assert "error" not in api.open_project(str(project.root))

    assert "error" not in api.save_draft("a.lsb", "u1", "mine", "orig")

    time.sleep(1.5)
    assert _changed_files(fake_window) == []


def test_the_editor_saving_on_its_way_to_the_dialog_leaves_the_disk_alone(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "blur-save", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "frontend draft")
    path = _change_on_disk(project, "disk value")
    _wait_for_conflict(fake_window)

    assert "error" in api.save_draft("a.lsb", "u1", "frontend draft")
    assert "error" not in api.accept_disk()

    assert load_string_file(path).by_id("u1").target == "disk value"


def test_keeping_the_editor_value_keeps_what_was_typed_after_the_conflict(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "typed-on", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "草稿")
    path = _change_on_disk(project, "changed by another program")
    _wait_for_conflict(fake_window)
    assert "error" not in api.begin_edit("a.lsb", "u1", "草稿二")

    assert "error" not in api.keep_draft()

    assert load_string_file(path).by_id("u1").target == "草稿二"


def test_a_conflict_outlasts_a_job_reloading_the_project(
    api: Api, tmp_path: Path, fake_window: FakeWindow
) -> None:
    project = _seed_project(api, tmp_path, "job-ends", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "草稿")
    path = _change_on_disk(project, "disk value")
    _wait_for_conflict(fake_window)

    api.state.reload()

    assert "error" not in api.keep_draft()
    assert load_string_file(path).by_id("u1").target == "草稿"
    assert api.state.pending_conflict is None


def test_a_conflict_raised_while_the_project_reloads_is_kept(
    api: Api, tmp_path: Path, monkeypatch
) -> None:
    project = _seed_project(api, tmp_path, "mid-reload", target="orig")
    assert "error" not in api.open_project(str(project.root))
    conflict = {"source_file": "a.lsb", "unit_id": "u1", "draft_target": "草稿"}

    def the_watcher_fires() -> None:
        api.state.pending_conflict = conflict

    monkeypatch.setattr(api.state, "refresh_glossary", the_watcher_fires)
    api.state.reload()

    assert api.state.pending_conflict == conflict


def test_opening_another_project_forgets_the_conflict(api: Api, tmp_path: Path) -> None:
    first = _seed_project(api, tmp_path, "first", target="orig")
    second = _seed_project(api, tmp_path, "second", target="orig")
    assert "error" not in api.open_project(str(first.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "草稿")
    api.state.pending_conflict = {"source_file": "a.lsb", "unit_id": "u1", "draft_target": "x"}

    assert "error" not in api.open_project(str(second.root))

    assert (api.state.pending_conflict, api.state.editing) == (None, None)


def test_a_keep_that_could_not_be_written_can_be_tried_again(
    api: Api, tmp_path: Path, fake_window: FakeWindow, monkeypatch
) -> None:
    project = _seed_project(api, tmp_path, "failed-keep", target="orig")
    assert "error" not in api.open_project(str(project.root))
    assert "error" not in api.begin_edit("a.lsb", "u1", "草稿")
    path = _change_on_disk(project, "disk value")
    _wait_for_conflict(fake_window)

    def locked(*args, **kwargs):
        raise PermissionError("the file is locked")

    with monkeypatch.context() as patch:
        patch.setattr(api_module, "save_target", locked)
        with pytest.raises(PermissionError):
            api.keep_draft()

    assert api.state.pending_conflict is not None
    assert "error" not in api.keep_draft()
    assert load_string_file(path).by_id("u1").target == "草稿"
