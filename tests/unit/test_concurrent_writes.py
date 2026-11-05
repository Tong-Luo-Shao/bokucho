"""An edit saved while a job works through the project survives the job."""

from __future__ import annotations

from threading import Thread
from types import SimpleNamespace

import pytest
from PIL import Image

from bokucho.application import image_editor_saving
from bokucho.application import text as text_mod
from bokucho.application.assets import (
    extract_images,
    held_image_units,
    load_image_units,
    save_image_units,
    set_images_ignored,
)
from bokucho.application.projects import (
    CreateProjectRequest,
    create_project,
    extract_text,
    string_filename,
)
from bokucho.application.quality import fix_brackets, fix_values
from bokucho.application.text import (
    import_translations,
    mark_reviewed,
    mark_units_reviewed,
    recheck_drafts,
    save_target,
    transform_project,
)
from bokucho.images import IMAGES_SOURCE_NAME, IMAGES_STRINGS_FILE, unit_id
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.sync.atomic import file_lock
from bokucho.sync.json_io import load_string_file
from bokucho.transform.base import Transform


def _project(tmp_path, files: dict[str, list[TextUnit]]) -> Project:
    project = create_project(
        CreateProjectRequest(tmp_path / "proj", "t", "ja", "zh-Hant", "livemaker")
    )
    game = tmp_path / "game"
    game.mkdir()
    (game / "MyGame.exe").write_bytes(b"")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={"game_path": str(game / "MyGame.exe")}
                )
            }
        )
    )
    project = Project.load(project.root)
    for source_file, units in files.items():
        project.save_string_file(
            StringFile(source_file=source_file, units=units), string_filename(source_file)
        )
    return project


def _on_disk(project: Project, source_file: str, unit_id: str) -> tuple[str, Status]:
    unit = load_string_file(project.strings_dir / string_filename(source_file)).by_id(unit_id)
    assert unit is not None
    return unit.target, unit.status


class _AfterFirstFile:
    """A progress sink that runs ``edit`` once the job has finished its first file."""

    def __init__(self, edit) -> None:
        self._edit = edit
        self._done = False

    def publish(self, event) -> None:
        if not self._done and event.completed == 1 and not event.transient:
            self._done = True
            self._edit()


def _two_files(second: list[TextUnit]) -> dict[str, list[TextUnit]]:
    return {
        "a.lsb": [TextUnit(id="a1", source="おはよう", target="早安", status=Status.MT)],
        "b.lsb": second,
    }


class _Exclaims(Transform):
    name = "exclaims"

    def convert(self, text: str) -> str:
        return text if text.endswith("！") else text + "！"


def test_transform_keeps_a_line_saved_while_it_runs(tmp_path):
    project = _project(
        tmp_path,
        _two_files(
            [
                TextUnit(id="b1", source="やあ", target="嗨", status=Status.MT),
                TextUnit(id="b2", source="ねえ"),
            ]
        ),
    )

    transform_project(
        project,
        [_Exclaims()],
        _AfterFirstFile(lambda: save_target(project, "b.lsb", "b2", "喂！")),
    )

    assert _on_disk(project, "b.lsb", "b1") == ("嗨！", Status.MT)
    assert _on_disk(project, "b.lsb", "b2") == ("喂！", Status.UNTRANSLATED)


def test_recheck_leaves_a_line_reviewed_while_it_runs(tmp_path):
    project = _project(
        tmp_path,
        _two_files([TextUnit(id="b1", source="おはよう", target="おはよう", status=Status.MT)]),
    )

    def review() -> None:
        save_target(project, "b.lsb", "b1", "早安")
        mark_reviewed(project, "b.lsb", "b1")

    recheck_drafts(project, _AfterFirstFile(review))

    assert _on_disk(project, "b.lsb", "b1") == ("早安", Status.REVIEWED)


def test_import_keeps_a_line_saved_while_it_runs(tmp_path):
    project = _project(
        tmp_path,
        _two_files([TextUnit(id="b1", source="おはよう"), TextUnit(id="b2", source="やあ")]),
    )

    import_translations(
        project,
        {"おはよう": "早安！"},
        _AfterFirstFile(lambda: save_target(project, "b.lsb", "b2", "嗨")),
    )

    assert _on_disk(project, "b.lsb", "b1") == ("早安！", Status.MT)
    assert _on_disk(project, "b.lsb", "b2") == ("嗨", Status.UNTRANSLATED)


def test_reviewing_many_keeps_a_line_saved_while_it_runs(tmp_path, monkeypatch):
    project = _project(
        tmp_path,
        _two_files(
            [
                TextUnit(id="b1", source="やあ", target="嗨", status=Status.MT),
                TextUnit(id="b2", source="ねえ"),
            ]
        ),
    )
    original = text_mod.record_transition
    calls = []

    def edits_on_first(unit, status, now=None):
        if not calls:
            save_target(project, "b.lsb", "b2", "喂")
        calls.append(unit.id)
        original(unit, status, now)

    monkeypatch.setattr(text_mod, "record_transition", edits_on_first)

    assert mark_units_reviewed(project, ["a1", "b1"], enforce_format=False).marked == 2

    assert _on_disk(project, "b.lsb", "b1") == ("嗨", Status.REVIEWED)
    assert _on_disk(project, "b.lsb", "b2") == ("喂", Status.UNTRANSLATED)


class _EditsOnSecondCheck:
    def __init__(self, edit) -> None:
        self._edit = edit
        self._checks = 0

    def add_cancel_callback(self, callback) -> None:
        del callback

    def raise_if_cancelled(self) -> None:
        self._checks += 1
        if self._checks == 2:
            self._edit()


def test_bracket_fix_keeps_a_line_saved_while_it_runs(tmp_path):
    project = _project(
        tmp_path,
        {
            "a.lsb": [TextUnit(id="a1", source="「あっ」", target="（啊）", status=Status.MT)],
            "b.lsb": [
                TextUnit(id="b1", source="「やめて」", target="（住手）", status=Status.MT),
                TextUnit(id="b2", source="ねえ"),
            ],
        },
    )

    fix_brackets(
        project, _EditsOnSecondCheck(lambda: save_target(project, "b.lsb", "b2", "喂"))
    )

    assert _on_disk(project, "b.lsb", "b1") == ("「住手」", Status.MT)
    assert _on_disk(project, "b.lsb", "b2") == ("喂", Status.UNTRANSLATED)


def test_value_fix_leaves_lines_edited_while_it_works_them_out(tmp_path):
    project = _project(
        tmp_path,
        _two_files(
            [
                TextUnit(id="b1", source="剣", target="劍", status=Status.MT),
                TextUnit(id="b2", source="ねえ"),
                TextUnit(id="b3", source="盾", target="盾", status=Status.MT),
            ]
        ),
    )

    def assemble(units):
        save_target(project, "b.lsb", "b2", "喂")
        save_target(project, "b.lsb", "b3", "盾牌")
        return {"b1": "長劍", "b3": "大盾"}

    assert fix_values(project, assemble).fixed == 1

    assert _on_disk(project, "b.lsb", "b1") == ("長劍", Status.MT)
    assert _on_disk(project, "b.lsb", "b2") == ("喂", Status.UNTRANSLATED)
    assert _on_disk(project, "b.lsb", "b3") == ("盾牌", Status.MT)


class _Adapter:
    def extract(self, game_path, sink, token):
        return [
            TextUnit(id=unit_id, source=source, engine_ref={"lsb": lsb, "at": unit_id})
            for lsb, unit_id, source in (
                ("a.lsb", "a1", "おはよう"),
                ("b.lsb", "b1", "やあ"),
                ("b.lsb", "b2", "ねえ"),
            )
        ]


def test_extraction_keeps_a_line_saved_while_it_runs(tmp_path):
    project = _project(
        tmp_path,
        _two_files([TextUnit(id="b1", source="やあ"), TextUnit(id="b2", source="ねえ")]),
    )

    extract_text(
        project,
        _Adapter(),
        _AfterFirstFile(lambda: save_target(project, "b.lsb", "b2", "喂")),
    )

    assert _on_disk(project, "a.lsb", "a1") == ("早安", Status.MT)
    assert _on_disk(project, "b.lsb", "b2") == ("喂", Status.UNTRANSLATED)


def _picture_project(tmp_path) -> Project:
    project = _project(tmp_path, {})
    game = tmp_path / "pictures"
    game.mkdir()
    for index in range(2):
        Image.new("RGB", (16, 8), (10, 20, 30)).save(game / f"{index:04d}.png")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": str(game)})
            }
        )
    )
    project = Project.load(project.root)
    project.save_string_file(
        StringFile(
            source_file=IMAGES_SOURCE_NAME,
            units=[
                TextUnit(
                    id=unit_id("0000.png"),
                    source="",
                    target="早安",
                    status=Status.MT,
                    kind="image",
                    engine_ref={"image": "0000.png", "format": "PNG"},
                )
            ],
        ),
        IMAGES_STRINGS_FILE,
    )
    return project


def test_a_scan_keeps_what_was_saved_while_it_read(tmp_path):
    project = _picture_project(tmp_path)
    calls = []

    def reader(path: str) -> str:
        calls.append(path)
        if len(calls) == 2:
            save_target(project, IMAGES_SOURCE_NAME, unit_id("0000.png"), "人工")
            set_images_ignored(project, ["0000.png"], True)
        return f"文字 {len(calls)}"

    extract_images(project, reader)

    units = load_image_units(project)
    first, second = units.by_id(unit_id("0000.png")), units.by_id(unit_id("0001.png"))
    assert (first.source, first.target, first.status) == ("文字 1", "人工", Status.IGNORED)
    assert second.source == "文字 2"


def _edit_lettering(project: Project, relative: str) -> None:
    """What the canvas editor saves: the picture's text and its boxes."""
    with held_image_units(project) as units:
        unit = units.by_id(unit_id(relative))
        if unit is None:
            unit = TextUnit(id=unit_id(relative), source="", kind="image")
            unit.engine_ref = {"image": relative}
            units.units.append(unit)
        unit.source = "人工"
        unit.engine_ref["regions"] = [{"box": [0, 0, 4, 4], "text": "人工"}]
        save_image_units(project, units)


def test_a_scan_keeps_lettering_edited_after_it_read_the_picture(tmp_path):
    project = _picture_project(tmp_path)
    calls = []

    def reader(path: str) -> str:
        calls.append(path)
        if len(calls) == 2:
            _edit_lettering(project, "0000.png")
        return f"文字 {len(calls)}"

    extract_images(project, reader)

    units = load_image_units(project)
    first, second = units.by_id(unit_id("0000.png")), units.by_id(unit_id("0001.png"))
    assert (first.source, first.engine_ref["regions"][0]["text"]) == ("人工", "人工")
    assert second.source == "文字 2"


def test_a_scan_reads_a_picture_given_only_a_translation_while_it_ran(tmp_path):
    project = _picture_project(tmp_path)
    calls = []

    def translate_the_unread_picture() -> None:
        with held_image_units(project) as units:
            units.units.append(
                TextUnit(
                    id=unit_id("0001.png"), source="", target="譯", kind="image",
                    engine_ref={"image": "0001.png"},
                )
            )
            save_image_units(project, units)

    def reader(path: str) -> str:
        calls.append(path)
        if len(calls) == 1:
            translate_the_unread_picture()
        return f"文字 {len(calls)}"

    extract_images(project, reader)

    unit = load_image_units(project).by_id(unit_id("0001.png"))
    assert (unit.source, unit.target) == ("文字 2", "譯")


def test_an_inventory_keeps_a_picture_lettered_while_it_ran(tmp_path):
    project = _picture_project(tmp_path)

    extract_images(project, sink=_AfterFirstFile(lambda: _edit_lettering(project, "0001.png")))

    unit = load_image_units(project).by_id(unit_id("0001.png"))
    assert (unit.source, unit.engine_ref["regions"][0]["text"]) == ("人工", "人工")


def test_a_transform_holds_each_file_against_a_save_until_it_is_written(tmp_path):
    project = _project(tmp_path, _two_files([TextUnit(id="b1", source="やあ")]))
    saving: list[Thread] = []

    class _SavesMidFile(_Exclaims):
        def convert(self, text: str) -> str:
            if not saving:
                saving.append(Thread(target=save_target, args=(project, "a.lsb", "a1", "早")))
                saving[0].start()
                saving[0].join(0.2)
            return super().convert(text)

    transform_project(project, [_SavesMidFile()])
    saving[0].join()

    assert _on_disk(project, "a.lsb", "a1") == ("早", Status.MT)


def test_ignoring_a_picture_waits_for_a_write_in_progress(tmp_path):
    project = _picture_project(tmp_path)
    path = project.strings_dir / IMAGES_STRINGS_FILE
    with file_lock(path):
        ignoring = Thread(target=set_images_ignored, args=(project, ["0000.png"], True))
        ignoring.start()
        ignoring.join(0.2)
        save_target(project, IMAGES_SOURCE_NAME, unit_id("0000.png"), "人工")
    ignoring.join()

    unit = load_image_units(project).by_id(unit_id("0000.png"))
    assert (unit.target, unit.status) == ("人工", Status.IGNORED)


def test_a_failed_editor_save_rolls_back_only_its_own_work(tmp_path, monkeypatch):
    project = _picture_project(tmp_path)
    ignoring = Thread(target=set_images_ignored, args=(project, ["0000.png"], True))

    def fails(*args):
        ignoring.start()
        ignoring.join(0.2)
        raise OSError("disk full")

    monkeypatch.setattr(image_editor_saving, "save_image_document", fails)
    with pytest.raises(OSError):
        image_editor_saving.save_image_editor_document(
            project, SimpleNamespace(relative_path="0000.png"), b"", b"", None, None
        )
    ignoring.join()

    assert load_image_units(project).by_id(unit_id("0000.png")).status is Status.IGNORED
