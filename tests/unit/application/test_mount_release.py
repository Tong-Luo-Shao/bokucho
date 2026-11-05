from __future__ import annotations

import json
import os
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.release import build_patch, release_patcher
from bokucho.engines.renpy import RenpyAdapter
from bokucho.engines.rpgmaker import RpgMakerAdapter
from bokucho.exe_icon import exe_icon
from bokucho.model import StringFile
from bokucho.patcher.payload import read_patch
from bokucho.project import Project

GAME = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"


@pytest.mark.parametrize("variant", ["mv", "mz"])
def test_rpgmaker_publishes_a_plugin_without_copied_game_data(tmp_path: Path, variant):
    game = tmp_path / "game"
    base = game / "www" if variant == "mv" else game
    (base / "data").mkdir(parents=True)
    (base / "js").mkdir()
    if variant == "mz":
        (base / "js" / "rmmz_core.js").write_text("// core", encoding="utf-8")
    system = base / "data" / "System.json"
    system.write_text(json.dumps({"gameTitle": "Original", "terms": {}}), encoding="utf-8")
    plugins = base / "js" / "plugins.js"
    plugins.write_text("var $plugins = [];", encoding="utf-8")
    before = {
        path.relative_to(game): path.read_bytes() for path in game.rglob("*") if path.is_file()
    }
    project = create_project(
        CreateProjectRequest(
            tmp_path / "project", "mount", "en", "zh-Hant", f"rpgmaker-{variant}",
        )
    )
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": str(game)}),
            }
        )
    )
    units = RpgMakerAdapter().extract(game)
    units[0].target = "翻譯"
    project.save_string_file(StringFile(source_file="System", units=units), "system.json")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ stub")
    built = build_patch(Project.load(project.root), stub=stub)
    assert built.executable == project.root / "dist" / "mount-en2zh-tw-patcher.exe"
    patch = read_patch(built.executable.read_bytes())
    assert patch.mount is not None
    with ZipFile(BytesIO(patch.mount)) as archive:
        prefix = "www/" if variant == "mv" else ""
        assert f"{prefix}js/plugins/Bokucho.js" in archive.namelist()
        assert not any(name.endswith("System.json") for name in archive.namelist())
        assert not any(name.endswith("plugins.js") for name in archive.namelist())
        metadata = json.loads(archive.read("mount.json"))
        assert metadata["registry"] == f"{prefix}js/plugins.js"
        assert f"{prefix}data/System.json" in metadata["checks"]
        script = archive.read(f"{prefix}js/plugins/Bokucho.js").decode("utf-8")
        line = next(line for line in script.splitlines() if "var translations = " in line)
        translations = json.loads(line.split(" = ", 1)[1].removesuffix(";"))
        assert translations["System.json"] == [
            [
                {
                    "path": ["gameTitle"],
                    "source": "Original",
                    "target": "翻譯",
                }
            ]
        ]
    assert before == {
        path.relative_to(game): path.read_bytes() for path in game.rglob("*") if path.is_file()
    }


def test_renpy_mounts_the_current_translation_instead_of_a_stale_overlay(tmp_path):
    game = tmp_path / "game"
    stubs = game / "game" / "tl" / "zh_hant"
    stubs.mkdir(parents=True)
    original = 'translate zh_hant strings:\n    old "Start"\n    new "Start"\n'
    (stubs / "menu.rpy").write_text(original, encoding="utf-8")
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "renpy", "en", "zh-Hant", "renpy")
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
    project = Project.load(project.root)
    adapter = RenpyAdapter(language="zh_hant")
    from bokucho.engines.renpy import parse_tl_source

    units = parse_tl_source(original, "menu.rpy")
    units[0].target = "開始"
    project.save_string_file(StringFile(source_file="menu.rpy", units=units), "menu.json")
    stale = project.root / "out" / "game" / "tl" / "zh_hant"
    stale.mkdir(parents=True)
    (stale / "menu.rpy").write_text("stale translation", encoding="utf-8")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ stub")
    built = release_patcher(project, lambda current: adapter, project.root / "dist", stub)
    assert built.executable.name == "renpy-en2zh-tw-patcher.exe"
    assert list(built.artifact_dir.iterdir()) == [built.executable]
    patch = read_patch(built.executable.read_bytes())
    assert patch.mount is not None
    with ZipFile(BytesIO(patch.mount)) as archive:
        assert json.loads(archive.read("mount.json")) == {"kind": "renpy", "language": "zh_hant"}
        translated = archive.read("game/tl/zh_hant/menu.rpy").decode("utf-8")
        assert 'new "開始"' in translated
        assert "stale translation" not in translated
        assert "config.language = 'zh_hant'" in archive.read("game/bokucho_language.rpy").decode()
    assert (stubs / "menu.rpy").read_text(encoding="utf-8") == original


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own resource writer draws the icon")
@pytest.mark.parametrize("engine", ["rpgmaker-mz", "renpy"])
def test_the_patch_shows_the_games_icon(tmp_path: Path, engine: str) -> None:
    game = tmp_path / "game"
    other = Path(sys.executable).read_bytes()
    if engine == "renpy":
        stubs = game / "game" / "tl" / "zh_hant"
        stubs.mkdir(parents=True)
        (stubs / "menu.rpy").write_text(
            'translate zh_hant strings:\n    old "Start"\n    new "Start"\n', encoding="utf-8"
        )
        (game / "Tale.py").write_text("", encoding="utf-8")
        program = game / "Tale.exe"
        (game / "Tale-32.exe").write_bytes(other)
    else:
        (game / "data").mkdir(parents=True)
        (game / "js").mkdir()
        (game / "js" / "rmmz_core.js").write_text("// core", encoding="utf-8")
        (game / "js" / "plugins.js").write_text("var $plugins = [];", encoding="utf-8")
        (game / "data" / "System.json").write_text(
            json.dumps({"gameTitle": "Original", "terms": {}}), encoding="utf-8"
        )
        program = game / "Game.exe"
        (game / "notification_helper.exe").write_bytes(other)
    program.write_bytes(GAME.read_bytes())
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "icon", "en", "zh-Hant", engine)
    )
    engine_config = project.config.engine.model_copy(
        update={"game_path": str(game), "language": "zh_hant"}
    )
    project.save_config(project.config.model_copy(update={"engine": engine_config}))
    if engine == "renpy":
        from bokucho.engines.renpy import parse_tl_source

        units = parse_tl_source((stubs / "menu.rpy").read_text(encoding="utf-8"), "menu.rpy")
    else:
        units = RpgMakerAdapter().extract(game)
    units[0].target = "翻譯"
    project.save_string_file(StringFile(source_file="strings", units=units), "strings.json")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(other)

    built = build_patch(Project.load(project.root), stub=stub)

    icon = exe_icon(built.executable.read_bytes())
    assert icon is not None
    assert icon == exe_icon(GAME.read_bytes())
