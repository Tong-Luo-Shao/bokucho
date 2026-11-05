"""Tests for unit history: status transitions and text a translation run replaces."""

import pytest

from bokucho.application import text as text_mod
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.text import (
    import_translations,
    mark_reviewed,
    mark_units_reviewed,
    record_transition,
    save_target,
    set_status,
    translate_project,
)
from bokucho.model import HISTORY_CAP, HistoryEntry, Status, StringFile, TextUnit
from bokucho.project import Project

FIXED_AT = "2026-07-22T00:00:00+00:00"


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(text_mod, "_utc_now", lambda: FIXED_AT)


def _project(tmp_path, *units: TextUnit):
    project = create_project(
        CreateProjectRequest(tmp_path / "proj", "t", "ja", "zh-Hant", "livemaker")
    )
    project.save_string_file(
        StringFile(source_file="a.lsb", units=list(units)), "a.lsb.json"
    )
    return _size_by_count(project)


def _size_by_count(project: Project) -> Project:
    """Batch by unit count, so these tests need no token counter."""
    translator = project.config.translator
    project.save_config(
        project.config.model_copy(
            update={
                "translator": translator.model_copy(
                    update={
                        "max_request_tokens": 0,
                        "interfaces": [
                            i.model_copy(update={"max_request_tokens": 0})
                            for i in translator.interfaces
                        ],
                    }
                )
            }
        )
    )
    return Project.load(project.root)


class FakeTranslator:
    def translate(self, batch, context, glossary=None):
        return ["草稿" for _unit in batch]


def _unit(project, unit_id="u1"):
    [string_file] = project.load_string_files()
    return string_file.by_id(unit_id)


def test_machine_translation_records_mt_entry(tmp_path, clock):
    project = _project(tmp_path, TextUnit(id="u1", source="はい"))
    translate_project(project, FakeTranslator())
    unit = _unit(project)
    assert unit.history == [
        HistoryEntry(target="草稿", status=Status.MT, at=FIXED_AT)
    ]


def test_translating_over_a_hand_draft_keeps_the_draft(tmp_path, clock):
    project = _project(tmp_path, TextUnit(id="u1", source="はい"))
    save_target(project, "a.lsb", "u1", "途中")
    translate_project(project, FakeTranslator())
    assert _unit(project).history == [
        HistoryEntry(target="途中", status=Status.UNTRANSLATED, at=FIXED_AT),
        HistoryEntry(target="草稿", status=Status.MT, at=FIXED_AT),
    ]


def test_retranslating_keeps_a_hand_edit_but_not_a_second_copy(tmp_path, clock):
    project = _project(
        tmp_path,
        TextUnit(id="u1", source="はい"),
        TextUnit(id="u2", source="いいえ"),
    )
    translate_project(project, FakeTranslator())
    save_target(project, "a.lsb", "u2", "手改")
    translate_project(project, FakeTranslator(), redo=True)
    mt = HistoryEntry(target="草稿", status=Status.MT, at=FIXED_AT)
    assert _unit(project, "u1").history == [mt]
    assert _unit(project, "u2").history == [
        mt,
        HistoryEntry(target="手改", status=Status.MT, at=FIXED_AT),
    ]


def test_saving_a_draft_records_no_history(tmp_path, clock):
    project = _project(tmp_path, TextUnit(id="u1", source="はい"))
    save_target(project, "a.lsb", "u1", "途中")
    assert _unit(project).history == []


def test_same_status_transition_records_nothing(clock):
    unit = TextUnit(
        id="u1",
        source="はい",
        target="草稿",
        status=Status.MT,
        history=[HistoryEntry(target="草稿", status=Status.MT, at=FIXED_AT)],
    )
    record_transition(unit, Status.MT)
    assert len(unit.history) == 1


def test_single_review_records_the_target_at_transition(tmp_path, clock):
    project = _project(
        tmp_path,
        TextUnit(id="u1", source="はい", target="定稿", status=Status.MT),
    )
    mark_reviewed(project, "a.lsb", "u1")
    assert _unit(project).history == [
        HistoryEntry(target="定稿", status=Status.REVIEWED, at=FIXED_AT)
    ]


def test_ignoring_clears_the_target_and_keeps_it_in_history(tmp_path, clock):
    project = _project(
        tmp_path,
        TextUnit(id="u1", source="はい", target="草稿", status=Status.MT),
    )
    set_status(project, "a.lsb", "u1", Status.IGNORED)
    unit = _unit(project)
    assert (unit.target, unit.status) == ("", Status.IGNORED)
    assert unit.history == [
        HistoryEntry(target="草稿", status=Status.MT, at=FIXED_AT),
        HistoryEntry(target="", status=Status.IGNORED, at=FIXED_AT),
    ]


def test_handing_an_ignored_line_back_restores_its_draft(tmp_path, clock):
    project = _project(
        tmp_path,
        TextUnit(id="u1", source="はい", target="草稿", status=Status.MT),
    )
    set_status(project, "a.lsb", "u1", Status.IGNORED)
    set_status(project, "a.lsb", "u1", Status.MT)
    unit = _unit(project)
    assert (unit.target, unit.status) == ("草稿", Status.MT)


def test_bulk_review_records_an_entry_per_unit(tmp_path, clock):
    project = _project(
        tmp_path,
        TextUnit(id="u1", source="一", target="甲", status=Status.MT),
        TextUnit(id="u2", source="二", target="乙", status=Status.MT),
    )
    mark_units_reviewed(project, ["u1", "u2"])
    [string_file] = project.load_string_files()
    for unit_id in ("u1", "u2"):
        history = string_file.by_id(unit_id).history
        assert [entry.status for entry in history] == [Status.REVIEWED]


def test_import_records_an_mt_entry(tmp_path, clock):
    project = _project(tmp_path, TextUnit(id="u1", source="はい"))
    import_translations(project, {"はい": "匯入"})
    assert _unit(project).history == [
        HistoryEntry(target="匯入", status=Status.MT, at=FIXED_AT)
    ]


def test_history_is_capped_and_oldest_dropped(clock):
    entries = [
        HistoryEntry(target=f"t{i}", status=Status.MT, at=FIXED_AT)
        for i in range(HISTORY_CAP)
    ]
    unit = TextUnit(
        id="u1", source="s", target="new", status=Status.MT, history=list(entries)
    )
    record_transition(unit, Status.REVIEWED)
    assert len(unit.history) == HISTORY_CAP
    assert unit.history[0].target == "t1"
    assert unit.history[-1].status is Status.REVIEWED
