"""A CatSystem2 patch adds the localization overlay beside the game and takes it away."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.release import build_patch
from bokucho.engines.cs2 import Cs2Adapter
from bokucho.model import StringFile
from bokucho.patcher.__main__ import main
from bokucho.patcher.core import PatchError, State, inspect
from bokucho.patcher.mount import (
    inspect_mount,
    install_mount,
    mount_kind,
    mount_text,
    restore_mount,
)
from bokucho.patcher.payload import Patch, read_patch
from bokucho.project import Project


def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _mount_zip(files: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("mount.json", json.dumps({"kind": "cs2"}))
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _bare_game(folder: Path) -> Path:
    """A Ren'Py-family game folder with no loose localization yet."""
    (folder / "game").mkdir(parents=True)
    (folder / "renpy").mkdir()
    return folder


def test_cs2_mount_installs_and_restores_through_the_dispatcher(tmp_path: Path):
    patch = Patch(
        "t",
        (),
        mount=_mount_zip(
            {
                "game/data/localization/zh_cn.json": _json({"text": {"01": {"6": "繁體一"}}}),
                "game/data/localization/system_prompts.json": _json(
                    {"messages": [{"id": "x", "zh_cn": "繁體訊息"}]}
                ),
            }
        ),
    )
    assert mount_kind(patch) == "cs2"
    assert "CatSystem2" in mount_text(patch).names

    player = _bare_game(tmp_path / "player")
    assert inspect_mount(player, patch).state is State.ORIGINAL
    install_mount(player, patch)
    zh = player / "game" / "data" / "localization" / "zh_cn.json"
    assert json.loads(zh.read_text(encoding="utf-8"))["text"]["01"]["6"] == "繁體一"
    assert inspect_mount(player, patch).state is State.PATCHED
    with pytest.raises(PatchError):  # a second apply refuses rather than doubling up
        install_mount(player, patch)

    assert restore_mount(player, patch) == []
    assert not zh.exists()
    assert not (player / "game" / "data").exists()  # empty dirs the patch made are gone
    assert (player / "game").is_dir()  # the game's own folder stays
    assert not (player / ".bokucho-backup").exists()


def test_cs2_mount_refuses_a_loose_overlay_it_would_clobber(tmp_path: Path):
    patch = Patch(
        "t", (), mount=_mount_zip({"game/data/localization/zh_cn.json": _json({"text": {}})})
    )
    player = _bare_game(tmp_path / "player")
    loc = player / "game" / "data" / "localization"
    loc.mkdir(parents=True)
    (loc / "zh_cn.json").write_text("{}", encoding="utf-8")
    assert inspect_mount(player, patch).state is State.UNKNOWN


def test_a_folder_without_renpy_is_not_a_cs2_game(tmp_path: Path):
    patch = Patch(
        "t", (), mount=_mount_zip({"game/data/localization/zh_cn.json": _json({"text": {}})})
    )
    folder = tmp_path / "p"
    (folder / "game").mkdir(parents=True)  # no renpy/ beside it
    assert inspect_mount(folder, patch).state is State.MISSING


def test_build_patch_produces_a_cs2_mount_that_the_patcher_applies(tmp_path: Path):
    source = _bare_game(tmp_path / "game")
    loc = source / "game" / "data" / "localization"
    loc.mkdir(parents=True)
    (loc / "ja.json").write_text(
        json.dumps({"text": {"01": {"6": "原文"}}, "ui": {"ok": "決定"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (loc / "zh_cn.json").write_text(
        json.dumps({"text": {"01": {"6": "原文译"}}, "ui": {"ok": "确定"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (source / "MyGame.exe").write_bytes(b"MZ game")

    project = create_project(
        CreateProjectRequest(tmp_path / "project", "story", "ja", "zh-Hant", "cs2")
    )
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(
                    update={"game_path": str(source), "prefill_lang": "zh_cn"}
                )
            }
        )
    )
    units = Cs2Adapter(source_lang="ja", prefill_lang="zh_cn").extract(source)
    for unit in units:
        if unit.id == "text/01/6":
            unit.target = "繁體正文"
    project.save_string_file(StringFile(source_file="cs2", units=units), "cs2.json")

    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ patcher")
    built = build_patch(Project.load(project.root), stub=stub)

    patch = read_patch(built.executable.read_bytes())
    assert mount_kind(patch) == "cs2"
    with ZipFile(BytesIO(patch.mount)) as archive:
        assert "game/data/localization/zh_cn.json" in archive.namelist()
        assert json.loads(archive.read("mount.json")) == {"kind": "cs2"}

    player = _bare_game(tmp_path / "玩家 copy")
    assert inspect(player, patch).state is State.ORIGINAL
    assert main([str(built.executable), "--apply", str(player)]) == 0
    zh = player / "game" / "data" / "localization" / "zh_cn.json"
    assert json.loads(zh.read_text(encoding="utf-8"))["text"]["01"]["6"] == "繁體正文"
    assert inspect(player, patch).state is State.PATCHED

    assert main([str(built.executable), "--restore", str(player)]) == 0
    assert not zh.exists()
    assert not (player / ".bokucho-backup").exists()
