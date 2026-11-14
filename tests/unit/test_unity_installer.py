"""A Unity patch adds BepInEx, XUnity and the translation, and takes them away again."""

from __future__ import annotations

import json
import shutil
import struct
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image

from bokucho.application import release
from bokucho.application.assets import extract_images
from bokucho.application.errors import InvalidProject
from bokucho.application.projects import (
    CreateProjectRequest,
    create_project,
    resolve_game_root,
)
from bokucho.application.release import build_patch
from bokucho.engines.unity import UnityAdapter, unit_id, xunity_hash
from bokucho.model import StringFile, TextUnit
from bokucho.patcher.__main__ import main
from bokucho.patcher.core import State, inspect
from bokucho.patcher.payload import read_patch
from bokucho.project import Project

TRANSLATION = "BepInEx/Translation/zh-TW/Text/Bokucho.txt"


def _pe(machine: int) -> bytes:
    image = bytearray(0x200)
    struct.pack_into("<I", image, 0x3C, 0x40)
    image[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", image, 0x44, machine)
    return bytes(image)


def _snapshot(folder: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(folder): path.read_bytes() for path in folder.rglob("*") if path.is_file()
    }


def _runtime(root: Path, *, il2cpp: bool = False) -> Path:
    loader = root / ("il2cpp-x64" if il2cpp else "x64")
    files = {
        "winhttp.dll": b"MZ doorstop",
        "doorstop_config.ini": b"[General]\nenabled=true\n",
        ".doorstop_version": b"4.3.0",
        "changelog.txt": b"BepInEx",
        "BepInEx/core/BepInEx.dll": b"MZ bepinex",
        "BepInEx/plugins/XUnity.AutoTranslator/XUnity.AutoTranslator.Plugin.Core.dll": b"MZ xu",
    }
    if il2cpp:
        files["BepInEx/core/BepInEx.Unity.IL2CPP.dll"] = b"MZ bepinex 6"
        files["dotnet/coreclr.dll"] = b"MZ dotnet"
    for name, data in files.items():
        path = loader / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def _game(root: Path, *, il2cpp: bool = False) -> Path:
    game = root / "game"
    data = game / "Game_Data"
    if il2cpp:
        (data / "il2cpp_data" / "Metadata").mkdir(parents=True)
        (data / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(b"\xaf\x1b\xb1\xfa")
        (game / "GameAssembly.dll").write_bytes(b"MZ code")
    else:
        (data / "Managed").mkdir(parents=True)
    (data / "globalgamemanagers").write_bytes(b"unity")
    (game / "UnityPlayer.dll").write_bytes(_pe(0x8664))
    (game / "Game.exe").write_bytes(b"MZ game")
    return game


def _project(tmp_path: Path, game: Path) -> Project:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "story", "ja", "zh-Hant", "unity")
    )
    project.save_config(
        project.config.model_copy(
            update={"engine": project.config.engine.model_copy(update={"game_path": str(game)})}
        )
    )
    units = [
        TextUnit(id=unit_id("はい"), source="はい", target="是", engine_ref={"file": "level0"}),
        TextUnit(id=unit_id("いいえ"), source="いいえ", engine_ref={"file": "level0"}),
    ]
    project.save_string_file(StringFile(source_file="level0", units=units), "level0.json")
    return Project.load(project.root)


@pytest.fixture
def unity_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: _runtime(tmp_path / "runtime"))
    game = _game(tmp_path)
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    built = build_patch(_project(tmp_path, game), stub=stub)
    player = tmp_path / "玩家 copy"
    shutil.copytree(game, player)
    return built.executable, player


def test_the_patch_carries_the_loader_and_the_translation(unity_release) -> None:
    executable, _ = unity_release
    patch = read_patch(executable.read_bytes())
    assert patch.mount is not None and patch.files == ()
    with ZipFile(BytesIO(patch.mount)) as archive:
        names = set(archive.namelist())
        assert {"winhttp.dll", "BepInEx/core/BepInEx.dll", TRANSLATION} <= names
        assert json.loads(archive.read("mount.json")) == {"kind": "unity", "data": "Game_Data"}
        assert archive.read(TRANSLATION).decode("utf-8").splitlines()[1:] == ["はい=是"]
        config = archive.read("BepInEx/config/AutoTranslatorConfig.ini").decode("utf-8")
        assert "Endpoint=\n" in config and "Language=zh-TW" in config


def _texture(name: str, color: str) -> SimpleNamespace:
    """A UnityPy-like Texture2D object."""
    data = SimpleNamespace(m_Name=name, image=Image.new("RGBA", (4, 2), color))
    return SimpleNamespace(read=lambda: data)


def test_the_patch_carries_each_translated_texture_under_its_xunity_name(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: _runtime(tmp_path / "runtime"))
    textures = [_texture("Title", "red"), _texture("Logo", "green")]
    monkeypatch.setattr(UnityAdapter, "_textures", staticmethod(lambda layout: textures))
    project = _project(tmp_path, _game(tmp_path))

    listed = extract_images(project)
    assert [entry["image"] for entry in listed.entries] == [
        "Logo [83FCE83274].png",
        "Title [768E0C1C69].png",
    ]
    unpacked = resolve_game_root(project)
    overlay = project.root / "out"
    overlay.mkdir()
    Image.new("RGBA", (4, 2), "blue").save(overlay / "Title [768E0C1C69].png")
    shutil.copyfile(unpacked / "Logo [83FCE83274].png", overlay / "Logo [83FCE83274].png")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")

    patch = read_patch(build_patch(project, stub=stub).executable.read_bytes())
    original = xunity_hash((unpacked / "Title [768E0C1C69].png").read_bytes())
    texture_dir = "BepInEx/Translation/zh-TW/Texture/"
    with ZipFile(BytesIO(patch.mount)) as archive:
        carried = [name for name in archive.namelist() if name.startswith(texture_dir)]
        assert carried == [f"{texture_dir}Title [768E0C1C69-{original}].png"]
        assert archive.read(carried[0]) == (overlay / "Title [768E0C1C69].png").read_bytes()
        config = archive.read("BepInEx/config/AutoTranslatorConfig.ini").decode("utf-8")
        assert "[Texture]\nEnableTextureTranslation=True\n" in config


def test_a_text_mesh_pro_game_gets_the_fallback_font_of_its_unity_year(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    (runtime / "fonts").mkdir()
    (runtime / "fonts" / "bokucho_tc_sdf_u2019").write_bytes(b"UnityFS font")
    (runtime / "fonts" / "bokucho_tc_sdf_u2021").write_bytes(b"UnityFS other")
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: runtime)
    game = _game(tmp_path)
    (game / "Game_Data" / "globalgamemanagers").write_bytes(b"\0" * 20 + b"2019.4.29f1\0")
    (game / "Game_Data" / "Managed" / "Unity.TextMeshPro.dll").write_bytes(b"MZ tmp")
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    patch = read_patch(build_patch(_project(tmp_path, game), stub=stub).executable.read_bytes())
    with ZipFile(BytesIO(patch.mount)) as archive:
        assert archive.read("BepInEx/fonts/bokucho_tc_sdf_u2019") == b"UnityFS font"
        assert "BepInEx/fonts/bokucho_tc_sdf_u2021" not in archive.namelist()
        config = archive.read("BepInEx/config/AutoTranslatorConfig.ini").decode("utf-8")
        assert "FallbackFontTextMeshPro=BepInEx/fonts/bokucho_tc_sdf_u2019\n" in config


def test_install_then_restore_leaves_the_game_as_it_was(unity_release, capsys) -> None:
    executable, player = unity_release
    before = _snapshot(player)
    patch = read_patch(executable.read_bytes())
    assert inspect(player, patch).state is State.ORIGINAL

    assert main([str(executable), "--apply", str(player)]) == 0
    assert (player / TRANSLATION).read_text(encoding="utf-8").endswith("はい=是\n")
    assert inspect(player, patch).state is State.PATCHED
    assert main([str(executable), "--apply", str(player)]) == 1

    # What BepInEx and XUnity write while the game runs, and another mod the player added.
    (player / "BepInEx" / "LogOutput.log").write_text("log", encoding="utf-8")
    (player / "BepInEx" / "config" / "AutoTranslatorConfig.ini").write_text("rewritten", "utf-8")
    (player / "BepInEx" / "config" / "BepInEx.cfg").write_text("[Logging]", "utf-8")
    redirector = player / "BepInEx" / "config" / "gravydevsupreme.xunity.resourceredirector.cfg"
    redirector.write_text("[Behaviour]", "utf-8")  # XUnity's second plugin, on first start
    other = player / "BepInEx" / "plugins" / "OtherMod" / "mod.dll"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"MZ mod")
    settings = player / "BepInEx" / "config" / "other.mod.cfg"
    settings.write_bytes(b"[Mod]")
    capsys.readouterr()

    assert main([str(executable), "--restore", str(player)]) == 0
    kept = capsys.readouterr().out
    assert "BepInEx/plugins/OtherMod/mod.dll" in kept and "BepInEx/config/other.mod.cfg" in kept
    assert _snapshot(player) == {
        **before,
        other.relative_to(player): b"MZ mod",
        settings.relative_to(player): b"[Mod]",
    }
    assert not (player / "winhttp.dll").exists()
    assert not (player / ".bokucho-backup").exists()


def test_a_game_that_already_has_bepinex_is_refused(unity_release) -> None:
    executable, player = unity_release
    (player / "BepInEx" / "plugins").mkdir(parents=True)
    before = _snapshot(player)
    patch = read_patch(executable.read_bytes())
    assert inspect(player, patch).state is State.UNKNOWN
    assert main([str(executable), "--apply", str(player)]) == 1
    assert _snapshot(player) == before


def test_a_folder_that_is_not_the_unity_game_is_not_offered(unity_release) -> None:
    executable, player = unity_release
    (player / "UnityPlayer.dll").unlink()
    assert inspect(player, read_patch(executable.read_bytes())).state is State.MISSING
    assert main([str(executable), "--apply", str(player)]) == 1


def test_a_missing_loader_is_refused_at_release(tmp_path, monkeypatch) -> None:
    game = _game(tmp_path)
    project = _project(tmp_path, game)
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: tmp_path / "nowhere")
    with pytest.raises(InvalidProject, match="no Unity loader"):
        build_patch(project, stub=stub)
    # A runtime with only the Mono loader has nothing for an IL2CPP game.
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: _runtime(tmp_path / "runtime"))
    (game / "GameAssembly.dll").write_bytes(b"MZ")
    with pytest.raises(InvalidProject, match="no Unity IL2CPP loader"):
        build_patch(project, stub=stub)


def test_a_game_without_unity_player_is_refused_at_release_in_words(
    tmp_path, monkeypatch
) -> None:
    # Older Unity builds the player into the game's program: detection still
    # finds the game, and the release said only that a file was missing.
    game = _game(tmp_path)
    (game / "UnityPlayer.dll").unlink()
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: _runtime(tmp_path / "runtime"))
    with pytest.raises(InvalidProject, match="has no UnityPlayer.dll"):
        build_patch(_project(tmp_path, game), stub=stub)


@pytest.fixture
def il2cpp_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    runtime = _runtime(tmp_path / "runtime")
    _runtime(runtime, il2cpp=True)
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: runtime)
    game = _game(tmp_path, il2cpp=True)
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    built = build_patch(_project(tmp_path, game), stub=stub)
    player = tmp_path / "玩家 copy"
    shutil.copytree(game, player)
    return built.executable, player


def test_an_il2cpp_patch_carries_bepinex_6_and_its_dotnet_runtime(il2cpp_release) -> None:
    executable, _ = il2cpp_release
    patch = read_patch(executable.read_bytes())
    with ZipFile(BytesIO(patch.mount)) as archive:
        names = set(archive.namelist())
        assert {"winhttp.dll", "dotnet/coreclr.dll", TRANSLATION} <= names
        assert "BepInEx/core/BepInEx.Unity.IL2CPP.dll" in names
        assert json.loads(archive.read("mount.json")) == {
            "kind": "unity", "data": "Game_Data", "backend": "il2cpp"
        }


def test_an_il2cpp_install_then_restore_leaves_the_game_as_it_was(il2cpp_release, capsys) -> None:
    executable, player = il2cpp_release
    before = _snapshot(player)
    patch = read_patch(executable.read_bytes())
    assert inspect(player, patch).state is State.ORIGINAL

    assert main([str(executable), "--apply", str(player)]) == 0
    assert (player / "dotnet" / "coreclr.dll").is_file()
    assert inspect(player, patch).state is State.PATCHED

    # What BepInEx 6 generates on the first start of an IL2CPP game.
    interop = player / "BepInEx" / "interop" / "Assembly-CSharp.dll"
    interop.parent.mkdir(parents=True)
    interop.write_bytes(b"MZ interop")
    (player / "BepInEx" / "unity-libs").mkdir()
    (player / "BepInEx" / "unity-libs" / "UnityEngine.dll").write_bytes(b"MZ base")
    capsys.readouterr()

    assert main([str(executable), "--restore", str(player)]) == 0
    assert _snapshot(player) == before
    assert not (player / "dotnet").exists()
    assert not (player / "BepInEx").exists()


def test_a_patch_for_the_other_backend_is_not_offered(il2cpp_release, tmp_path) -> None:
    executable, player = il2cpp_release
    patch = read_patch(executable.read_bytes())
    (player / "GameAssembly.dll").unlink()  # now a Mono build: BepInEx 6 IL2CPP cannot load it
    assert inspect(player, patch).state is State.MISSING
    assert main([str(executable), "--apply", str(player)]) == 1


def test_a_project_that_names_the_game_executable_still_gets_its_patch(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(release, "bundled_unity_runtime", lambda: _runtime(tmp_path / "runtime"))
    game = _game(tmp_path)
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    built = build_patch(_project(tmp_path, game / "Game.exe"), stub=stub)
    patch = read_patch(built.executable.read_bytes())
    with ZipFile(BytesIO(patch.mount)) as archive:
        assert json.loads(archive.read("mount.json")) == {"kind": "unity", "data": "Game_Data"}
