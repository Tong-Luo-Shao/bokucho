"""End-to-end checks against a real KiriKiri game; nothing is written to its folder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bokucho.application.contracts import NeverCancelled
from bokucho.application.projects import extract_text
from bokucho.application.release import _kirikiri_patch
from bokucho.engines import xp3
from bokucho.engines.kag import KagRules, decode_script, encode_script, pieces, rewrite
from bokucho.engines.kirikiri import KiriKiriAdapter, game_layout, scripts
from bokucho.infrastructure.factories import make_engine_adapter
from bokucho.patcher.recipe import compile_recipe
from bokucho.project import Project

GAME = (
    Path(os.environ["BOKUCHO_TEST_KIRIKIRI"]) if os.environ.get("BOKUCHO_TEST_KIRIKIRI") else None
)
SKIP = [s for s in os.environ.get("BOKUCHO_TEST_KIRIKIRI_SKIP", "").split(",") if s]
NAMES = [s for s in os.environ.get("BOKUCHO_TEST_KIRIKIRI_NAMES", "").split(",") if s]
RULES = KagRules.from_config(SKIP, NAMES)

pytestmark = pytest.mark.skipif(
    GAME is None, reason="set BOKUCHO_TEST_KIRIKIRI to a KiriKiri game folder"
)


def _snapshot() -> dict[Path, tuple[int, int]]:
    assert GAME is not None
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in GAME.rglob("*")}


@pytest.fixture(autouse=True)
def _game_folder_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "the game folder changed"


def test_every_script_reads_and_rewrites_unchanged():
    layout = game_layout(GAME)
    assert layout.slots in {"loop", "single"}
    found = scripts(GAME, layout.archives)
    assert found
    for name, source in found.items():
        data = xp3.read(GAME / source.archive, source.entry)
        text = decode_script(data)
        same = {p.id: p.source for p in pieces(text, name, RULES)}
        if data.startswith(b"\xff\xfe"):
            assert encode_script(rewrite(text, name, RULES, same)) == data, name


def test_extract_gives_lines_with_branches_skipped():
    units = KiriKiriAdapter(SKIP, NAMES).extract(GAME)
    assert any(u.kind == "dialogue" for u in units)
    assert not NAMES or any(u.kind == "name" for u in units)
    assert len({u.id for u in units}) == len(units), "ids must be unique"


def test_the_recipe_compiles_into_scratch_only(tmp_path, new_project):
    project = new_project(engine="kirikiri", root=tmp_path / "project")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={
                        "game_path": str(GAME),
                        "kag_skip_branches": SKIP,
                        "kag_name_attributes": NAMES,
                    }
                )
            }
        )
    )
    project = Project.load(project.root)
    extract_text(project, make_engine_adapter(project))
    marked = None
    for string_file in project.load_string_files():
        for unit in string_file.units:
            if marked is None and unit.kind == "dialogue":
                unit.target = unit.source + "（測）"
                marked = unit
        project.save_string_file(string_file, f"{string_file.source_file}.json")
    assert marked is not None
    work = tmp_path / "work"
    work.mkdir()
    patch = _kirikiri_patch(project, work, NeverCancelled())
    rebuilt, added = compile_recipe(GAME, patch, tmp_path / "root")
    assert rebuilt == {}
    (archive,) = added
    built = tmp_path / archive.name
    built.write_bytes(archive.data)
    entries = {entry.name: entry for entry in xp3.entries(built)}
    script = marked.engine_ref["file"]
    assert set(entries) == {script}
    text = decode_script(xp3.read(built, entries[script]))
    assert any(p.source == marked.target for p in pieces(text, script, RULES))
