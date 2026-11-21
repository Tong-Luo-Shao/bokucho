"""Releasing a KiriKiri game: a recipe the player's patcher compiles into its own archive."""

from __future__ import annotations

import io
import json
import shutil
from zipfile import ZipFile

import pytest

from bokucho.application.errors import ReleaseBlocked
from bokucho.application.projects import extract_text
from bokucho.application.release import release_patcher
from bokucho.engines import xp3
from bokucho.engines.kag import decode_script, encode_script
from bokucho.infrastructure.factories import make_engine_adapter
from bokucho.patcher import core
from bokucho.patcher.payload import read_patch
from bokucho.project import Project


def _project(tmp_path, new_project, game, **engine):
    project = new_project(engine="kirikiri")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={"game_path": str(game), "kag_name_attributes": ["name.fn"], **engine}
                )
            }
        )
    )
    project = Project.load(project.root)
    extract_text(project, make_engine_adapter(project))
    return project


def _translate(project, targets):
    for string_file in project.load_string_files():
        for unit in string_file.units:
            unit.target = targets.get(unit.id, "")
        project.save_string_file(string_file, f"{string_file.source_file}.json")


def _release(project, stub):
    return release_patcher(project, make_engine_adapter, project.root / "dist", stub)


@pytest.fixture
def kirikiri_project(tmp_path, new_project, kirikiri_game):
    def build(**game_options):
        game = kirikiri_game(
            tmp_path / "game", {"scenario/first.ks": "[name fn=春香]\nおはよう\n"}, **game_options
        )
        project = _project(tmp_path, new_project, game)
        _translate(project, {"first.ks::0": "早安"})
        stub = tmp_path / "stub.exe"
        stub.write_bytes(b"MZ patcher")
        return project, game, stub

    return build


def test_the_patch_carries_only_translations_and_adds_an_archive(kirikiri_project, tmp_path):
    project, game, stub = kirikiri_project()
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["data.xp3"]
    with ZipFile(io.BytesIO(patch.recipe)) as recipe:
        names = recipe.namelist()
        meta = json.loads(recipe.read("recipe.json"))
    assert all(
        n == "recipe.json" or (n.startswith("strings/") and n.endswith(".json")) for n in names
    )
    assert meta["engine"] == "kirikiri"
    assert meta["slots"] == "loop"
    assert meta["archives"] == ["data.xp3"]
    assert meta["kag_name_attributes"] == ["name.fn"]
    assert "original_sha256" not in project.project_file.read_text(encoding="utf-8")
    player = tmp_path / "player"
    shutil.copytree(game, player)
    core.apply(player, patch)
    assert (player / "patch.xp3").is_file()
    assert core.inspect(player, patch).state is core.State.PATCHED


def test_a_skipped_label_keeps_its_lines_and_names_in_the_applied_patch(
    tmp_path, new_project, kirikiri_game
):
    script = "*a_jp\n[name fn=春香]\nおはよう\n*a_chh\n[name fn=春香]\n早安\n"
    game = kirikiri_game(tmp_path / "game", {"scenario/first.ks": script})
    project = _project(tmp_path, new_project, game, kag_skip_labels=["*_chh"])
    _translate(project, {"first.ks:a_jp:0": "你好", "name:春香": "小春"})
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    core.apply(game, read_patch(_release(project, stub).executable.read_bytes()))
    (entry,) = xp3.entries(game / "patch.xp3")
    assert decode_script(xp3.read(game / "patch.xp3", entry)) == (
        '*a_jp\n[name fn="小春"]\n你好\n*a_chh\n[name fn=春香]\n早安\n'
    )


def test_the_release_leaves_the_game_folder_as_it_was(kirikiri_project):
    project, game, stub = kirikiri_project()
    before = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
    _release(project, stub)
    after = {p.relative_to(game): p.read_bytes() for p in game.rglob("*") if p.is_file()}
    assert after == before


def test_a_game_that_loads_no_patch_archive_is_not_released(kirikiri_project):
    project, _game, stub = kirikiri_project(slots="none")
    with pytest.raises(ReleaseBlocked, match="does not load patch archives"):
        _release(project, stub)


def test_a_game_with_a_bokucho_patch_on_it_is_not_released(kirikiri_project):
    project, game, stub = kirikiri_project()
    (game / core.RECORD).write_text("{}", encoding="utf-8")
    with pytest.raises(ReleaseBlocked, match="holds a patch already"):
        _release(project, stub)


def test_a_publisher_patch_archive_is_checked_with_the_game(kirikiri_project):
    project, _game, stub = kirikiri_project(
        extra={"patch.xp3": {"first.ks": encode_script("[name fn=春香]\nおはよう\n")}}
    )
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["data.xp3", "patch.xp3"]


def test_an_archive_without_a_script_the_game_reads_is_not_checked(kirikiri_project):
    project, _game, stub = kirikiri_project(extra={"patch.xp3": {"note.txt": b"x"}})
    patch = read_patch(_release(project, stub).executable.read_bytes())
    assert [f.name for f in patch.files] == ["data.xp3"]


def test_a_speaker_name_with_a_quote_is_not_released(kirikiri_project):
    project, _game, stub = kirikiri_project()
    _translate(project, {"first.ks::0": "早安", "name:春香": '春"香'})
    with pytest.raises(ReleaseBlocked, match="name:春香"):
        _release(project, stub)


def test_a_release_with_nothing_translated_is_not_released(kirikiri_project):
    project, _game, stub = kirikiri_project()
    _translate(project, {})
    with pytest.raises(ReleaseBlocked, match="No script has a translated line"):
        _release(project, stub)
