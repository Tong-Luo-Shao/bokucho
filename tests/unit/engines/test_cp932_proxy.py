"""Tests for Phase 5: CP932 proxy map, patch-runtime, build-patch."""

import io
import json
import zipfile

import pytest
from typer.testing import CliRunner

from bokucho import cli
from bokucho.application.release import build_proxy_map
from bokucho.engines.cp932_proxy import MissingGlyphError, ProxyMap, cp932_ok
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project

runner = CliRunner()




def test_cp932_ok():
    assert cp932_ok("あ") and cp932_ok("結")
    assert not cp932_ok("您") and not cp932_ok("貓")


def test_proxy_encode_decode_round_trip():
    pm = ProxyMap()
    pm.extend({"您", "貓"}, reserved=set("本次承蒙遊玩"))
    encoded = pm.encode("承蒙您遊玩，貓")
    assert "您" not in encoded and "貓" not in encoded
    assert encoded.encode("cp932")
    assert pm.decode(encoded) == "承蒙您遊玩，貓"


def test_proxy_missing_glyph_raises():
    with pytest.raises(MissingGlyphError):
        ProxyMap().encode("您")


def test_proxy_extend_is_stable_and_respects_reserved():
    pm = ProxyMap()
    pm.extend({"您"}, reserved=set())
    donor = pm.real2proxy["您"]
    added = pm.extend({"您", "們"}, reserved={donor})
    assert added == 1
    assert pm.real2proxy["您"] == donor
    assert pm.real2proxy["們"] != donor


def _chinese_writes(character: str) -> bool:
    for codec in ("cp950", "gb2312"):
        try:
            character.encode(codec)
            return True
        except UnicodeEncodeError:
            continue
    return False


def test_new_donors_are_kanji_chinese_text_does_not_write():
    pm = ProxyMap()
    pm.extend({"您", "們", "貓", "们"}, reserved=set())
    assert not any(_chinese_writes(donor) for donor in pm.proxy2real)


def test_a_thousand_donors_take_no_common_japanese_kanji():
    needed = set()
    for code in range(0x4E00, 0xA000):
        if len(needed) == 1000:
            break
        try:
            chr(code).encode("cp932")
        except UnicodeEncodeError:
            needed.add(chr(code))
    pm = ProxyMap()
    pm.extend(needed, reserved=set())
    level_one = [
        donor
        for donor in pm.proxy2real
        if 0x889F <= int.from_bytes(donor.encode("cp932"), "big") <= 0x9872
    ]
    assert level_one == []


def test_a_donor_the_text_now_writes_is_given_back(tmp_path):
    greeting = TextUnit(id="u1", source="ようこそ", target="您好", status=Status.MT)
    project = _project(tmp_path, [greeting])
    build_proxy_map(project)
    donor = ProxyMap.load(project.root / "proxy_map.json").real2proxy["您"]
    literal = TextUnit(id="u2", source="色", target=f"{donor}色", status=Status.MT)
    project.save_string_file(
        StringFile(source_file="a.lsb", units=[greeting, literal]), "a.lsb.json"
    )

    result = build_proxy_map(project)

    proxy = ProxyMap.load(project.root / "proxy_map.json")
    assert result.repaired == 1
    assert proxy.real2proxy["您"] not in {donor, *"ようこそ您好色"}
    assert proxy.decode(proxy.encode(literal.target)) == literal.target




def _project(tmp_path, units) -> Project:
    proj_dir = tmp_path / "proj"
    assert runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"]).exit_code == 0
    project = Project.load(proj_dir)
    project.save_string_file(StringFile(source_file="a.lsb", units=units), "a.lsb.json")
    return project


def test_patch_runtime_maps_each_non_cp932_character_once(tmp_path):
    project = _project(
        tmp_path,
        [TextUnit(id="u1", source="ようこそ", target="您好，貓", status=Status.MT)],
    )

    result = runner.invoke(cli.app, ["patch-runtime", str(project.root)])
    assert result.exit_code == 0, result.stdout
    mapping = json.loads((project.root / "proxy_map.json").read_text(encoding="utf-8"))
    assert set(mapping) == {"您", "貓"}

    assert "0 new" in runner.invoke(cli.app, ["patch-runtime", str(project.root)]).stdout


@pytest.mark.usefixtures("no_rehearsal")
def test_build_patch_writes_the_patcher_with_translation_data_only(tmp_path, monkeypatch):
    from bokucho.engines.base import EngineAdapter
    from bokucho.patcher.payload import read_patch
    from bokucho.project import FontSourceConfig

    class TextOnlyAdapter(EngineAdapter):
        def extract(self, game_path, sink=None, token=None):
            return []

        def inject(self, units, game_path, out_path, sink=None, token=None):
            raise AssertionError("a LiveMaker patch is compiled on the player's machine")

    project = _project(
        tmp_path,
        [TextUnit(id="u1", source="ようこそ", target="您好", status=Status.MT)],
    )
    (project.root / "Game.exe").write_bytes(b"MZ original game")
    (project.root / "LM_Chinese.ttf").write_bytes(b"fake font")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": "Game.exe"}),
                "font": project.config.font.model_copy(
                    update={"sources": [FontSourceConfig(path="LM_Chinese.ttf")]}
                ),
            }
        )
    )
    stub = tmp_path / "bokucho-patcher.exe"
    stub.write_bytes(b"MZ stub")
    monkeypatch.setattr("bokucho.application.release.patcher_stub", lambda: stub)
    monkeypatch.setattr(cli, "make_engine_adapter", lambda current: TextOnlyAdapter())

    result = runner.invoke(cli.app, ["build-patch", str(project.root)])
    assert result.exit_code == 0, result.stdout

    executable = project.root / "dist" / "t-ja2zh-tw-patcher.exe"
    assert executable.is_file()
    patch = read_patch(executable.read_bytes())
    with zipfile.ZipFile(io.BytesIO(patch.recipe)) as archive:
        names = archive.namelist()
    assert set(names) == {"recipe.json", "proxy_map.json", "strings/a.lsb.json", "fonts/0.ttf"}
    assert json.loads((project.root / "proxy_map.json").read_text(encoding="utf-8"))


def test_build_patch_without_extract_errors(tmp_path):
    proj_dir = tmp_path / "empty"
    runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"])
    assert runner.invoke(cli.app, ["build-patch", str(proj_dir)]).exit_code == 1
