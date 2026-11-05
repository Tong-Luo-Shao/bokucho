"""A released LiveMaker recipe compiles only against the player's original."""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from livemaker.archive import LMArchive
from livemaker.lpb import LMProject

from bokucho.application.errors import JobCancelled, ReleaseBlocked
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.release import release_patcher
from bokucho.engines.cp932_proxy import ProxyMap
from bokucho.engines.livemaker import LiveMakerAdapter
from bokucho.engines.livepixelfont import HEADER_END, MAGIC, NAME_AT, NAME_SIZE, RECORD_FORMAT
from bokucho.model import StringFile
from bokucho.patcher import core
from bokucho.patcher.payload import read_patch
from bokucho.project import FontConfig, FontSourceConfig, Project


@pytest.fixture
def runtime_project(tmp_path, make_font, livemaker_script):
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "runtime", "ja", "zh-Hant", "livemaker")
    )
    source = tmp_path / "source"
    source.mkdir()
    script = livemaker_script("scene.lsb")
    script.lsb.command_params = [[] for _ in range(64)]
    script.runs('name = "こんにちは"')
    script.runs("AddArray(_tmp, name)")
    (source / "scene.lsb").write_bytes(script.lsb.to_lsb())
    donor = "鉷"
    index = struct.pack(
        RECORD_FORMAT, int.from_bytes(donor.encode("cp932"), "big"), 18, 18, 0, 16, 20, 0, 0
    )
    compressed = zlib.compress(index)
    header = (
        MAGIC
        + struct.pack("<III", 100, len(compressed), 20 * 18)
        + b"GameFont".ljust(NAME_SIZE, b"\0")
        + bytes(HEADER_END - NAME_AT - NAME_SIZE)
    )
    (source / "font.lft").write_bytes(header + compressed + bytes(20 * 18))
    (source / "picture.gal").write_bytes(b"original picture")
    game = tmp_path / "Game.dat"
    with LMArchive(name=game, mode="w") as archive:
        archive.write(source / "scene.lsb", arcname="scene.lsb")
        archive.write(source / "font.lft", arcname="font.lft")
        archive.write(source / "picture.gal", arcname="picture.gal")
    units = LiveMakerAdapter().extract(source)
    assert units
    units[0].target = "您"
    project.save_string_file(StringFile(source_file="scene.lsb", units=units), "scene.json")
    ProxyMap({"您": donor}).save(project.root / "proxy_map.json")
    face = make_font(tmp_path / "source.ttf", "Test", "您")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": str(game)}),
                "font": FontConfig(sources=[FontSourceConfig(path=str(face))]),
            }
        )
    )
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    return Project.load(project.root), game, stub


def _adapter(project):
    return LiveMakerAdapter(project.root / "proxy_map.json", project.root / "unpacked")


def test_a_recipe_compiles_at_apply_and_restores_the_original(runtime_project, tmp_path):
    project, game, stub = runtime_project
    original = game.read_bytes()
    built = release_patcher(project, _adapter, project.root / "dist", stub)
    patch = read_patch(built.executable.read_bytes())
    assert patch.recipe is not None
    assert not (project.root / "out").exists()
    assert list(built.artifact_dir.iterdir()) == [built.executable]
    player = tmp_path / "player"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    core.apply(player, patch)
    assert (player / game.name).read_bytes() != original
    with LMArchive(player / game.name) as archive:
        script = tmp_path / "translated.lsb"
        script.write_bytes(archive.read("scene.lsb"))
    assert "鉷" in str(LiveMakerAdapter._open(script).commands[0]["Calc"])
    assert core.inspect(player, patch).state is core.State.PATCHED
    with pytest.raises(core.PatchError, match="已經套用"):
        core.apply(player, patch)
    assert core.restore(player, patch) == []
    assert (player / game.name).read_bytes() == original
    assert sorted(path.name for path in player.iterdir()) == [game.name]


def test_a_patch_the_players_patcher_would_refuse_is_not_released(runtime_project, tmp_path):
    project, game, stub = runtime_project
    loader = _system_game(game, tmp_path)
    strings = project.load_string_files()[0]
    strings.units[0].target = "您妳"
    project.save_string_file(strings, "scene.json")

    with pytest.raises(ReleaseBlocked, match="妳"):
        release_patcher(project, _adapter, project.root / "dist", stub, uif=loader)

    assert not list((project.root / "dist").rglob("*.exe"))
    assert Project.load(project.root).config.engine.original_sha256 is None


class _CancelledOnceRehearsing:
    def __init__(self, dist):
        self.dist = dist

    def add_cancel_callback(self, callback):
        pass

    def raise_if_cancelled(self):
        if any(self.dist.rglob("rehearsal")):
            raise JobCancelled("cancelled")


def test_cancelling_while_the_patch_is_rehearsed_stops_the_release(runtime_project):
    project, _game, stub = runtime_project
    dist = project.root / "dist"

    with pytest.raises(JobCancelled):
        release_patcher(project, _adapter, dist, stub, token=_CancelledOnceRehearsing(dist))

    assert list(dist.iterdir()) == []
    assert Project.load(project.root).config.engine.original_sha256 is None


def test_a_failed_compilation_leaves_the_original_untouched(runtime_project, tmp_path):
    project, game, stub = runtime_project
    built = release_patcher(project, _adapter, project.root / "dist", stub)
    patch = read_patch(built.executable.read_bytes())
    broken = io.BytesIO()
    with ZipFile(io.BytesIO(patch.recipe)) as source, ZipFile(broken, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name.startswith("strings/"):
                strings = json.loads(data)
                strings["units"][0]["engine_ref"]["lsb"] = "missing.lsb"
                data = json.dumps(strings).encode()
            target.writestr(name, data)
    patch = replace(patch, recipe=broken.getvalue())
    player = tmp_path / "player"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    with pytest.raises(core.PatchError, match="編譯"):
        core.apply(player, patch)
    assert (player / game.name).read_bytes() == game.read_bytes()
    assert sorted(path.name for path in player.iterdir()) == [game.name]


def test_the_recipe_carries_typeset_pictures_and_excludes_stale_scripts(runtime_project, tmp_path):
    project, game, stub = runtime_project
    overlay = project.root / "out"
    overlay.mkdir()
    (overlay / "scene.lsb").write_bytes(b"stale compiled game script")
    (overlay / "picture.gal").write_bytes(b"translated picture")
    built = release_patcher(project, _adapter, project.root / "dist", stub)
    patch = read_patch(built.executable.read_bytes())
    with ZipFile(io.BytesIO(patch.recipe)) as archive:
        assert "out/scene.lsb" not in archive.namelist()
        assert archive.read("out/picture.gal") == b"translated picture"
    player = tmp_path / "player"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    core.apply(player, patch)
    with LMArchive(player / game.name) as archive:
        assert archive.read("picture.gal") == b"translated picture"
        assert archive.read("scene.lsb") != b"stale compiled game script"


def _system_game(game, tmp_path):
    with LMArchive(game) as archive:
        script = archive.read("scene.lsb")
    settings = LMProject(
        init_lsb="scene.lsb",
        system_settings=[{"name": "StatusFontName", "type": "Str", "value": "MS Gothic Font"}],
    ).to_lpb()
    settings_path = tmp_path / "live.lpb"
    settings_path.write_bytes(settings)
    script_path = tmp_path / "scene.lsb"
    script_path.write_bytes(script)
    with LMArchive(name=game, mode="w") as archive:
        archive.write(script_path, arcname="scene.lsb")
        archive.write(settings_path, arcname="live.lpb")
    loader = tmp_path / "winmm.dll"
    loader.write_bytes(b"MZ test UIF loader")
    return loader


def test_a_system_font_recipe_installs_and_restores_its_runtime_files(runtime_project, tmp_path):
    project, game, stub = runtime_project
    loader = _system_game(game, tmp_path)
    original = game.read_bytes()
    built = release_patcher(project, _adapter, project.root / "dist", stub, uif=loader)
    patch = read_patch(built.executable.read_bytes())
    player = tmp_path / "player"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    core.apply(player, patch)
    assert (player / "winmm.dll").read_bytes() == loader.read_bytes()
    assert len(list(player.glob("Bokucho-*.ttf"))) == 1
    assert "フォント選択" in core.applied_message(patch, player)
    core.restore(player, patch)
    assert (player / game.name).read_bytes() == original
    assert sorted(path.name for path in player.iterdir()) == [game.name]


def test_a_recipe_cannot_write_outside_its_workspace(runtime_project, tmp_path):
    project, game, stub = runtime_project
    built = release_patcher(project, _adapter, project.root / "dist", stub)
    patch = read_patch(built.executable.read_bytes())
    stream = io.BytesIO(patch.recipe)
    with ZipFile(stream, "a") as archive:
        archive.writestr("../victim.txt", b"changed")
    patch = replace(patch, recipe=stream.getvalue())
    player = tmp_path / "player"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    with pytest.raises(core.PatchError, match="Unsafe recipe path"):
        core.apply(player, patch)
    assert (player / game.name).read_bytes() == game.read_bytes()
    assert sorted(path.name for path in player.iterdir()) == [game.name]
    assert not (player / "victim.txt").exists()


def test_an_existing_temporary_file_is_preserved(runtime_project, tmp_path):
    project, game, stub = runtime_project
    built = release_patcher(project, _adapter, project.root / "dist", stub)
    patch = read_patch(built.executable.read_bytes())
    player = tmp_path / "player"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    temporary = player / f"{game.name}.bokucho-tmp"
    temporary.write_bytes(b"keep this earlier work")
    with pytest.raises(core.PatchError, match="已經存在"):
        core.apply(player, patch)
    assert temporary.read_bytes() == b"keep this earlier work"
    assert (player / game.name).read_bytes() == game.read_bytes()
    assert not (player / core.RECORD).exists()


@pytest.mark.parametrize("system_font", [False, True])
def test_the_patcher_compiles_without_the_language_detector(
    runtime_project, tmp_path, system_font
):
    project, game, stub = runtime_project
    loader = _system_game(game, tmp_path) if system_font else None
    built = release_patcher(project, _adapter, project.root / "dist", stub, uif=loader)
    player = tmp_path / "player"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    blocked = (
        "import sys; sys.modules['lingua'] = None; "
        "from bokucho.patcher.__main__ import main; raise SystemExit(main(sys.argv[1:]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", blocked, str(built.executable), "--apply", str(player)],
        capture_output=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert core.inspect(player, read_patch(built.executable.read_bytes())).state is (
        core.State.PATCHED
    )


@pytest.mark.skipif(
    not os.environ.get("BOKUCHO_TEST_PATCHER"), reason="needs a freshly built patcher"
)
@pytest.mark.parametrize("system_font", [False, True])
def test_the_frozen_patcher_compiles_without_an_installed_bokucho(
    runtime_project, tmp_path, system_font
):
    project, game, _ = runtime_project
    loader = _system_game(game, tmp_path) if system_font else None
    built = release_patcher(
        project,
        _adapter,
        project.root / "dist",
        Path(os.environ["BOKUCHO_TEST_PATCHER"]),
        uif=loader,
    )
    player = tmp_path / "玩家遊戲"
    player.mkdir()
    shutil.copy2(game, player / game.name)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    completed = subprocess.run(
        [str(built.executable), "--apply", str(player)],
        cwd=player,
        env=environment,
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    patch = read_patch(built.executable.read_bytes())
    assert core.inspect(player, patch).state is core.State.PATCHED
    core.restore(player, patch)
    assert (player / game.name).read_bytes() == game.read_bytes()
