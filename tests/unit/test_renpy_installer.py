"""A Ren'Py patch adds the translation beside the game and takes it away again."""

from __future__ import annotations

import json
import shutil
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.release import build_patch
from bokucho.engines.renpy import parse_tl_source
from bokucho.model import StringFile
from bokucho.patcher.__main__ import main
from bokucho.patcher.core import State, describe, inspect
from bokucho.patcher.payload import read_patch
from bokucho.project import Project

STUB = 'translate zh_hant strings:\n    old "Start"\n    new "Start"\n'


def _snapshot(folder: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()
    }


@pytest.fixture
def renpy_release(tmp_path: Path) -> tuple[Path, Path]:
    game = tmp_path / "game"
    stubs = game / "game" / "tl" / "zh_hant"
    stubs.mkdir(parents=True)
    (stubs / "menu.rpy").write_text(STUB, encoding="utf-8")
    (game / "game" / "script.rpy").write_text('label start:\n    "Start"\n', encoding="utf-8")
    (game / "renpy").mkdir()
    (game / "renpy" / "__init__.py").write_text("", encoding="utf-8")
    (game / "MyGame.exe").write_bytes(b"MZ game")
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "story", "en", "zh-Hant", "renpy")
    )
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={"game_path": str(game), "language": "zh_hant"}
                ),
            }
        )
    )
    units = parse_tl_source(STUB, "menu.rpy")
    units[0].target = "開始"
    project.save_string_file(StringFile(source_file="menu.rpy", units=units), "menu.json")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    built = build_patch(Project.load(project.root), stub=stub)
    assert built.executable.name == "story-en2zh-tw-patcher.exe"
    player = tmp_path / "玩家 copy"
    shutil.copytree(game, player)
    shutil.rmtree(player / "game" / "tl")
    return built.executable, player


def test_the_patch_carries_only_the_translation_and_its_language(renpy_release) -> None:
    executable, _ = renpy_release
    patch = read_patch(executable.read_bytes())
    assert patch.mount is not None
    with ZipFile(BytesIO(patch.mount)) as archive:
        assert sorted(archive.namelist()) == [
            "game/bokucho_language.rpy",
            "game/tl/zh_hant/menu.rpy",
            "mount.json",
        ]
        assert json.loads(archive.read("mount.json")) == {"kind": "renpy", "language": "zh_hant"}


def test_the_executable_installs_the_translation_and_restores_exact_bytes(renpy_release) -> None:
    executable, player = renpy_release
    before = _snapshot(player)
    patch = read_patch(executable.read_bytes())
    assert inspect(player, patch).state is State.ORIGINAL

    assert main([str(executable), "--apply", str(player)]) == 0
    menu = player / "game" / "tl" / "zh_hant" / "menu.rpy"
    assert 'new "開始"' in menu.read_text(encoding="utf-8")
    language = (player / "game" / "bokucho_language.rpy").read_text(encoding="utf-8")
    assert "config.language = 'zh_hant'" in language
    assert inspect(player, patch).state is State.PATCHED
    assert main([str(executable), "--apply", str(player)]) == 1

    menu.with_name("menu.rpyc").write_bytes(b"compiled")
    (player / "game" / "bokucho_language.rpyc").write_bytes(b"compiled")
    assert main([str(executable), "--restore", str(player)]) == 0
    assert _snapshot(player) == before
    assert not (player / "game" / "tl").exists()
    assert not (player / ".bokucho-backup").exists()


def test_a_game_with_its_own_translation_in_that_language_is_refused(renpy_release) -> None:
    executable, player = renpy_release
    official = player / "game" / "tl" / "zh_hant" / "official.rpy"
    official.parent.mkdir(parents=True)
    official.write_text("translate zh_hant start_1:\n    pass\n", encoding="utf-8")
    before = _snapshot(player)
    patch = read_patch(executable.read_bytes())
    assert inspect(player, patch).state is State.UNKNOWN
    assert "Ren'Py" in describe(inspect(player, patch), patch)

    assert main([str(executable), "--apply", str(player)]) == 1
    assert _snapshot(player) == before


def test_a_folder_that_is_not_a_renpy_game_is_not_offered(renpy_release) -> None:
    executable, player = renpy_release
    shutil.rmtree(player / "renpy")
    patch = read_patch(executable.read_bytes())
    assert inspect(player, patch).state is State.MISSING
    assert main([str(executable), "--apply", str(player)]) == 1


def test_restore_keeps_a_script_the_player_edited(renpy_release, capsys) -> None:
    executable, player = renpy_release
    assert main([str(executable), "--apply", str(player)]) == 0
    menu = player / "game" / "tl" / "zh_hant" / "menu.rpy"
    menu.write_text("edited by the player", encoding="utf-8")
    assert main([str(executable), "--restore", str(player)]) == 0
    assert "game/tl/zh_hant/menu.rpy" in capsys.readouterr().out
    assert menu.read_text(encoding="utf-8") == "edited by the player"
    assert not (player / "game" / "bokucho_language.rpy").exists()
    assert not (player / ".bokucho-backup").exists()


def test_an_install_that_fails_partway_leaves_the_game_as_it_was(renpy_release, capsys) -> None:
    executable, player = renpy_release
    (player / "game" / "tl").write_bytes(b"not a folder")
    before = _snapshot(player)
    assert main([str(executable), "--apply", str(player)]) == 1
    assert "遊戲檔案沒有被改動" in capsys.readouterr().err
    assert _snapshot(player) == before
    assert not (player / ".bokucho-backup").exists()
