"""Tests for finding and previewing a replace across translations."""

import pytest

from bokucho.application.errors import ValidationFailure
from bokucho.application.glossary import list_glossary_entries, upsert_glossary
from bokucho.application.replace import (
    apply_replace,
    preview_replace,
    replace_in_target,
    undo_replace,
)
from bokucho.model import Status, StringFile, TextUnit


def _project(new_project, units, engine="renpy"):
    project = new_project(engine=engine)
    project.save_string_file(StringFile(source_file="scene", units=units), "scene.json")
    return project


def _unit(unit_id, target, status=Status.MT, **more):
    return TextUnit(id=unit_id, source=f"source {unit_id}", target=target, status=status, **more)


def test_every_occurrence_between_markup_is_replaced_and_marked():
    after, before_marks, after_marks = replace_in_target("光與光", "光", "小光", "renpy")
    assert after == "小光與小光"
    assert before_marks == ((0, 1), (2, 3))
    assert after_marks == ((0, 2), (3, 5))


def test_markup_is_never_searched():
    after, before_marks, _ = replace_in_target("{b}光{/b}問[光]", "光", "曜", "renpy")
    assert after == "{b}曜{/b}問[光]"
    assert before_marks == ((3, 4),)


def test_find_is_matched_as_typed_not_as_a_pattern():
    assert replace_in_target("a.*b (光) x", ".*", "-", "")[0] == "a-b (光) x"
    assert replace_in_target("a.*b (光) x", "(光)", "曜", "")[0] == "a.*b 曜 x"
    assert replace_in_target("abc", ".*", "-", "")[0] == "abc"


def test_overlapping_occurrences_are_taken_left_to_right():
    assert replace_in_target("aaa", "aa", "b", "")[0] == "ba"


def test_the_preview_lists_lines_of_every_status_and_writes_nothing(new_project):
    project = _project(new_project, [
        _unit("1", "光來了"),
        _unit("2", "光走了", Status.REVIEWED),
        _unit("3", "沒有"),
        _unit("4", "", Status.UNTRANSLATED),
    ])
    preview = preview_replace(project, "光", "曜")
    assert [(m.unit_id, m.before, m.after, m.status) for m in preview.matches] == [
        ("1", "光來了", "曜來了", Status.MT),
        ("2", "光走了", "曜走了", Status.REVIEWED),
    ]
    assert preview.matches[0].source_file == "scene"
    saved = [unit.target for unit in project.load_string_files()[0].units]
    assert saved == ["光來了", "光走了", "沒有", ""]


def test_a_replacement_that_holds_the_find_text_lands_once(new_project):
    project = _project(new_project, [_unit("1", "光與光")])
    assert preview_replace(project, "光", "小光").matches[0].after == "小光與小光"


def test_a_line_that_would_become_empty_is_counted_and_not_matched(new_project):
    project = _project(new_project, [_unit("1", "光"), _unit("2", "光來了")])
    preview = preview_replace(project, "光", "")
    assert [m.unit_id for m in preview.matches] == ["2"]
    assert preview.emptied == 1


def test_pictures_are_named_and_not_matched(new_project):
    project = _project(new_project, [
        _unit("p", "光之塔", kind="image", engine_ref={"image": "ui/title.png"}),
        _unit("1", "光來了"),
    ])
    preview = preview_replace(project, "光", "曜")
    assert [m.unit_id for m in preview.matches] == ["1"]
    assert preview.pictures == ("ui/title.png",)


def test_glossary_entries_holding_the_old_rendering_are_listed(new_project):
    project = _project(new_project, [_unit("1", "光來了")])
    upsert_glossary(project, "ヒカリ", "光")
    upsert_glossary(project, "ヒカリの剣", "光之劍", scope="global")
    upsert_glossary(project, "ヨル", "夜")
    glossary = preview_replace(project, "光", "曜").glossary
    assert [(c.source, c.before, c.after, c.origin) for c in glossary] == [
        ("ヒカリ", "光", "曜", "project"),
        ("ヒカリの剣", "光之劍", "曜之劍", "global"),
    ]


def test_the_fingerprint_moves_when_a_matching_line_does(new_project):
    project = _project(new_project, [_unit("1", "光來了")])
    first = preview_replace(project, "光", "曜").fingerprint
    assert preview_replace(project, "光", "曜").fingerprint == first
    project.save_string_file(
        StringFile(source_file="scene", units=[_unit("1", "光來了嗎")]), "scene.json"
    )
    assert preview_replace(project, "光", "曜").fingerprint != first


@pytest.mark.parametrize(("find", "replacement", "said"), [
    ("", "曜", "Type the text to find."),
    ("光", "光", "Find and Replace with are the same."),
])
def test_a_replace_with_nothing_to_do_is_refused(new_project, find, replacement, said):
    with pytest.raises(ValidationFailure, match=said):
        preview_replace(_project(new_project, [_unit("1", "光")]), find, replacement)


def test_a_replacement_that_newly_fails_the_format_check_is_flagged(new_project):
    project = _project(new_project, [
        TextUnit(id="1", source="ヒカリが来た", target="光來了", status=Status.MT),
    ])
    assert preview_replace(project, "光", "ひかり").matches[0].breaks_format is True
    assert preview_replace(project, "光", "曜").matches[0].breaks_format is False


def _targets(project):
    return {u.id: (u.target, u.status) for u in project.load_string_files()[0].units}


def _apply(project, find, replacement, **more):
    fingerprint = preview_replace(project, find, replacement).fingerprint
    return apply_replace(project, find, replacement, fingerprint, **more)


def test_applying_rewrites_the_lines_and_keeps_status_and_history(new_project):
    project = _project(new_project, [
        _unit("1", "光與光"),
        _unit("2", "光走了", Status.REVIEWED),
        _unit("3", "沒有"),
    ])
    result = _apply(project, "光", "曜")
    assert (result.lines, result.occurrences) == (2, 3)
    assert _targets(project) == {
        "1": ("曜與曜", Status.MT),
        "2": ("曜走了", Status.REVIEWED),
        "3": ("沒有", Status.MT),
    }
    reviewed = project.load_string_files()[0].units[1]
    assert reviewed.history[-1].target == "光走了"


def test_a_line_left_out_and_a_picture_keep_their_text(new_project):
    project = _project(new_project, [
        _unit("1", "光來了"), _unit("2", "光走了"),
        _unit("p", "光之塔", kind="image", engine_ref={"image": "ui/title.png"}),
    ])
    result = _apply(project, "光", "曜", excluded=[("scene", "2")])
    assert result.lines == 1
    assert [t for t, _ in _targets(project).values()] == ["曜來了", "光走了", "光之塔"]


def test_a_replace_is_refused_when_the_translations_changed_since_the_preview(new_project):
    project = _project(new_project, [_unit("1", "光來了")])
    stale = preview_replace(project, "光", "曜").fingerprint
    project.save_string_file(
        StringFile(source_file="scene", units=[_unit("1", "光來了"), _unit("2", "光走了")]),
        "scene.json",
    )
    with pytest.raises(ValidationFailure, match="changed since the preview"):
        apply_replace(project, "光", "曜", stale)
    assert _targets(project)["2"][0] == "光走了"


def test_the_glossary_follows_and_the_shared_one_is_not_written(new_project):
    project = _project(new_project, [_unit("1", "光來了")])
    upsert_glossary(project, "ヒカリ", "光")
    upsert_glossary(project, "ヒカリの剣", "光之劍", scope="global")
    result = _apply(project, "光", "曜", glossary=["ヒカリ", "ヒカリの剣"])
    assert result.glossary == 2
    listed = {(e.source, e.target, origin) for e, origin in list_glossary_entries(project)}
    assert listed == {
        ("ヒカリ", "曜", "project"),
        ("ヒカリの剣", "曜之劍", "project"),
        ("ヒカリの剣", "光之劍", "shadowed"),
    }


def test_the_glossary_can_follow_with_every_line_left_out(new_project):
    project = _project(new_project, [_unit("1", "光來了")])
    upsert_glossary(project, "ヒカリ", "光")
    result = _apply(project, "光", "曜", excluded=[("scene", "1")], glossary=["ヒカリ"])
    assert (result.lines, result.glossary) == (0, 1)
    assert _targets(project)["1"][0] == "光來了"


def test_undo_restores_what_still_holds_the_replaced_text(new_project):
    project = _project(new_project, [_unit("1", "光來了"), _unit("2", "光走了")])
    upsert_glossary(project, "ヒカリ", "光")
    upsert_glossary(project, "ヒカリの剣", "光之劍", scope="global")
    result = _apply(project, "光", "小光", glossary=["ヒカリ", "ヒカリの剣"])
    string_file = project.load_string_files()[0]
    string_file.units[1].target = "小光真的走了"
    project.save_string_file(string_file, "scene.json")

    undone = undo_replace(project, result.undo)

    assert (undone.restored, undone.skipped, undone.glossary) == (1, 1, 2)
    assert [t for t, _ in _targets(project).values()] == ["光來了", "小光真的走了"]
    listed = {(e.source, e.target, origin) for e, origin in list_glossary_entries(project)}
    assert listed == {("ヒカリ", "光", "project"), ("ヒカリの剣", "光之劍", "global")}


def test_a_shared_entry_keeps_its_note_in_the_project_override(new_project):
    project = _project(new_project, [_unit("1", "光來了")])
    upsert_glossary(project, "ヒカリ", "光", note="the heroine", scope="global")
    _apply(project, "光", "曜", glossary=["ヒカリ"])
    override = next(e for e, origin in list_glossary_entries(project) if origin == "project")
    assert (override.target, override.note) == ("曜", "the heroine")


def test_undo_counts_a_line_whose_file_is_gone_as_skipped(new_project):
    project = _project(new_project, [_unit("1", "光來了")])
    result = _apply(project, "光", "曜")
    (project.strings_dir / "scene.json").unlink()
    undone = undo_replace(project, result.undo)
    assert (undone.restored, undone.skipped) == (0, 1)
