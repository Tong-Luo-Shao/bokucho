"""A KiriKiri patch is compiled on the player's PC into an archive added beside the game's."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.engines import xp3
from bokucho.engines.kag import decode_script
from bokucho.engines.kirikiri import KiriKiriAdapter, game_layout
from bokucho.model import StringFile
from bokucho.patcher import core
from bokucho.patcher.payload import FileState, Patch, PatchedFile
from bokucho.project import Project


def _patch(project: Project, game: Path) -> Patch:
    layout = game_layout(game)
    engine = project.config.engine
    recipe = {
        "engine": "kirikiri",
        "name": project.config.name,
        "source_lang": project.config.source_lang,
        "target_lang": project.config.target_lang,
        "archives": list(layout.archives),
        "slots": layout.slots,
        "kag_skip_branches": engine.kag_skip_branches or [],
        "kag_name_attributes": engine.kag_name_attributes or [],
    }
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("recipe.json", json.dumps(recipe, ensure_ascii=False))
        for path in sorted(project.strings_dir.glob("*.json")):
            archive.writestr(f"strings/{path.name}", path.read_bytes())
    files = tuple(
        PatchedFile(
            name,
            FileState((game / name).stat().st_size, core.sha256_of(game / name)),
            None,
            b"",
            0,
        )
        for name in layout.archives
    )
    return Patch("test", files, recipe=buffer.getvalue())


def _translated(tmp_path: Path, kirikiri_game, **game_options) -> tuple[Project, Path]:
    game = kirikiri_game(tmp_path / "game", {"scenario/first.ks": "おはよう\n"}, **game_options)
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "Sample", "ja", "zh-Hant", "kirikiri")
    )
    project.config.engine.game_path = str(game)
    units = KiriKiriAdapter().extract(game)
    units[0].target = "早安"
    project.save_string_file(StringFile(source_file="first.ks", units=units), "first.ks.json")
    return project, game


@pytest.fixture
def translated(tmp_path, kirikiri_game):
    return _translated(tmp_path, kirikiri_game)


def test_applying_adds_a_patch_archive_and_leaves_the_game_archive(translated):
    project, game = translated
    before = (game / "data.xp3").read_bytes()
    core.apply(game, _patch(project, game))
    assert (game / "data.xp3").read_bytes() == before
    assert not (game / "data.xp3.bak").exists()
    (entry,) = xp3.entries(game / "patch.xp3")
    assert entry.name == "first.ks"
    assert decode_script(xp3.read(game / "patch.xp3", entry)) == "早安\n"


def test_an_applied_game_reads_as_applied_and_is_not_applied_twice(translated):
    project, game = translated
    patch = _patch(project, game)
    core.apply(game, patch)
    assert core.inspect(game, patch) == core.FolderState(core.State.PATCHED, True)
    with pytest.raises(core.PatchError, match="已經套用"):
        core.apply(game, patch)


def test_a_removed_patch_archive_reads_as_original_again(translated):
    project, game = translated
    patch = _patch(project, game)
    core.apply(game, patch)
    (game / "patch.xp3").unlink()
    assert core.inspect(game, patch).state is core.State.ORIGINAL


def test_restoring_takes_the_archive_away(translated):
    project, game = translated
    names = sorted(p.name for p in game.iterdir())
    patch = _patch(project, game)
    core.apply(game, patch)
    assert core.restore(game, patch) == []
    assert sorted(p.name for p in game.iterdir()) == names
    assert core.inspect(game, patch).state is core.State.ORIGINAL


def test_an_existing_patch_archive_makes_ours_the_next(tmp_path, kirikiri_game):
    project, game = _translated(tmp_path, kirikiri_game, extra={"patch.xp3": {"note.txt": b"x"}})
    before = (game / "patch.xp3").read_bytes()
    core.apply(game, _patch(project, game))
    assert (game / "patch.xp3").read_bytes() == before
    (entry,) = xp3.entries(game / "patch2.xp3")
    assert decode_script(xp3.read(game / "patch2.xp3", entry)) == "早安\n"


def test_a_patch_archive_the_game_does_not_load_refuses_the_apply(translated):
    project, game = translated
    patch = _patch(project, game)
    (game / "patch3.xp3").write_bytes(b"someone else's")
    with pytest.raises(core.PatchError, match="patch3.xp3"):
        core.apply(game, patch)
    assert not (game / "patch.xp3").exists()
    assert not (game / core.RECORD).exists()


def test_a_changed_game_archive_is_refused_with_nothing_written(translated):
    project, game = translated
    patch = _patch(project, game)
    (game / "data.xp3").write_bytes((game / "data.xp3").read_bytes() + b"\0")
    names = sorted(p.name for p in game.iterdir())
    with pytest.raises(core.PatchError, match="不是這個補丁對應的原版"):
        core.apply(game, patch)
    assert sorted(p.name for p in game.iterdir()) == names


def test_a_stray_backup_beside_a_game_archive_does_not_matter(translated):
    project, game = translated
    (game / "data.xp3.bak").write_bytes(b"something else")
    core.apply(game, _patch(project, game))
    assert (game / "patch.xp3").is_file()
    assert (game / "data.xp3.bak").read_bytes() == b"something else"


@pytest.mark.parametrize("slots", ["loop", "single"])
def test_a_game_holding_an_older_version_is_restored_by_the_newer_one(
    tmp_path, kirikiri_game, slots
):
    project, game = _translated(tmp_path, kirikiri_game, slots=slots)
    names = sorted(p.name for p in game.iterdir())
    older = _patch(project, game)
    (string_file,) = project.load_string_files()
    string_file.units[0].target = "晚安"
    project.save_string_file(string_file, "first.ks.json")
    newer = _patch(project, game)
    core.apply(game, older)

    found = core.inspect(game, newer)
    assert found == core.FolderState(core.State.UNKNOWN, True)
    assert "另一個版本" in core.describe(found, newer)
    with pytest.raises(core.PatchError, match="另一個版本"):
        core.apply(game, newer)
    assert core.restore(game, newer) == []
    assert sorted(p.name for p in game.iterdir()) == names
    core.apply(game, newer)
    (entry,) = xp3.entries(game / "patch.xp3")
    assert decode_script(xp3.read(game / "patch.xp3", entry)) == "晚安\n"


def test_the_disk_check_counts_only_what_is_added(tmp_path, kirikiri_game, monkeypatch):
    project, game = _translated(tmp_path, kirikiri_game, padding=16 << 20)
    monkeypatch.setattr(
        core.shutil, "disk_usage", lambda _: SimpleNamespace(free=core.SPARE_BYTES + (8 << 20))
    )
    core.apply(game, _patch(project, game))
    assert (game / "patch.xp3").is_file()
