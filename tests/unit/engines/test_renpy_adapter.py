"""Tests for the Ren'Py engine adapter (SDK-stub parsing and filling)."""

import io
import json
import zipfile

import pytest
from typer.testing import CliRunner

from bokucho import cli
from bokucho.engines.renpy import (
    RenpyAdapter,
    default_language,
    fill_tl_source,
    parse_tl_source,
)

DIALOGUE_STUB = '''\
# TODO: Translation updated at 2026-07-20 10:00

# game/script.rpy:27
translate zh_hant start_63dd7a26:

    # e "You've created a new Ren'Py game."
    e "You've created a new Ren'Py game."

# game/script.rpy:29
translate zh_hant start_de3c7360:

    # "It said \\"hello\\".\\nTwice."
    "It said \\"hello\\".\\nTwice."
'''

STRINGS_STUB = '''\
translate zh_hant strings:

    # game/script.rpy:14
    old "Start"
    new "Start"

    # game/script.rpy:15
    old "Quit"
    new "Quit"
'''


def test_default_language_sanitizes_tag():
    assert default_language("zh-Hant") == "zh_hant"
    assert default_language("ja") == "ja"


def test_parse_dialogue_blocks():
    units = parse_tl_source(DIALOGUE_STUB, "script.rpy")
    assert [u.id for u in units] == ["start_63dd7a26", "start_de3c7360"]
    assert units[0].source == "You've created a new Ren'Py game."
    assert units[0].engine_ref == {"file": "script.rpy", "kind": "dialogue"}
    assert units[0].status == "untranslated"
    assert units[1].source == 'It said "hello".\nTwice.'


def test_parse_strings_blocks_and_dedupe():
    seen: set[str] = set()
    units = parse_tl_source(STRINGS_STUB, "script.rpy", seen)
    assert [u.source for u in units] == ["Start", "Quit"]
    assert units[0].engine_ref == {
        "file": "script.rpy",
        "kind": "strings",
        "old": "Start",
    }
    assert units[0].id.startswith("strings_")
    again = parse_tl_source(STRINGS_STUB, "common.rpy", seen)
    assert again == []


def test_parse_skips_unparsable_block_with_warning(capsys):
    bad = "translate zh_hant broken_00000000:\n\n    # only a comment\n"
    units = parse_tl_source(bad, "script.rpy")
    assert units == []
    assert "broken_00000000" in capsys.readouterr().err


def test_parse_ignores_python_and_style_blocks():
    stub = (
        "translate zh_hant python:\n"
        '    gui.text_font = "font.ttf"\n'
        "translate zh_hant style default:\n"
        '    font "font.ttf"\n'
    )
    assert parse_tl_source(stub, "gui.rpy") == []


def test_fill_dialogue_and_strings():
    filled = fill_tl_source(
        DIALOGUE_STUB, {"start_63dd7a26": "你建立了新遊戲。"}, {}
    )
    assert '    e "你建立了新遊戲。"' in filled
    assert 'It said \\"hello\\".\\nTwice.' in filled
    assert '# e "You\'ve created a new Ren\'Py game."' in filled

    filled = fill_tl_source(STRINGS_STUB, {}, {"Start": "開始"})
    assert '    new "開始"' in filled
    assert '    old "Start"' in filled
    assert '    new "Quit"' in filled


def test_fill_escapes_target():
    filled = fill_tl_source(
        DIALOGUE_STUB, {"start_63dd7a26": '說了"哈囉"。\n兩次。'}, {}
    )
    assert '    e "說了\\"哈囉\\"。\\n兩次。"' in filled


def test_fill_is_idempotent():
    once = fill_tl_source(DIALOGUE_STUB, {"start_63dd7a26": "你建立了新遊戲。"}, {})
    twice = fill_tl_source(once, {"start_63dd7a26": "你建立了新遊戲。"}, {})
    assert once == twice


def test_fill_without_translations_is_identity():
    assert fill_tl_source(DIALOGUE_STUB, {}, {}) == DIALOGUE_STUB
    assert fill_tl_source(STRINGS_STUB, {}, {}) == STRINGS_STUB


def _fake_game(tmp_path, stub_text=DIALOGUE_STUB):
    game = tmp_path / "mygame"
    tl = game / "game" / "tl" / "zh_hant"
    tl.mkdir(parents=True)
    (tl / "script.rpy").write_text(stub_text, encoding="utf-8")
    return game


def test_extract_parses_generated_stubs(tmp_path, monkeypatch):
    game = _fake_game(tmp_path)
    adapter = RenpyAdapter(sdk_path=tmp_path / "sdk", language="zh_hant")
    monkeypatch.setattr(RenpyAdapter, "_run_sdk_translate", lambda self, gp: None)
    units = adapter.extract(game)
    assert [u.id for u in units] == ["start_63dd7a26", "start_de3c7360"]


def test_extract_without_sdk_raises_guidance(tmp_path):
    adapter = RenpyAdapter(sdk_path="", language="zh_hant")
    with pytest.raises(FileNotFoundError, match="renpy.org"):
        adapter.extract(_fake_game(tmp_path))


def test_extract_no_stubs_raises(tmp_path, monkeypatch):
    game = tmp_path / "empty"
    game.mkdir()
    adapter = RenpyAdapter(sdk_path=tmp_path / "sdk", language="zh_hant")
    monkeypatch.setattr(RenpyAdapter, "_run_sdk_translate", lambda self, gp: None)
    with pytest.raises(RuntimeError, match="no stub files"):
        adapter.extract(game)


def test_inject_writes_filled_copies_and_leaves_game_untouched(tmp_path, monkeypatch):
    game = _fake_game(tmp_path)
    adapter = RenpyAdapter(sdk_path=tmp_path / "sdk", language="zh_hant")
    monkeypatch.setattr(RenpyAdapter, "_run_sdk_translate", lambda self, gp: None)
    units = adapter.extract(game)
    units[0].target = "你建立了新遊戲。"
    units[0].status = "mt"

    out = tmp_path / "out"
    adapter.inject(units, game, out)

    dest = out / "game" / "tl" / "zh_hant" / "script.rpy"
    assert '    e "你建立了新遊戲。"' in dest.read_text(encoding="utf-8")
    src = game / "game" / "tl" / "zh_hant" / "script.rpy"
    assert src.read_text(encoding="utf-8") == DIALOGUE_STUB


def test_inject_strings_units(tmp_path, monkeypatch):
    game = _fake_game(tmp_path, STRINGS_STUB)
    adapter = RenpyAdapter(sdk_path=tmp_path / "sdk", language="zh_hant")
    monkeypatch.setattr(RenpyAdapter, "_run_sdk_translate", lambda self, gp: None)
    units = adapter.extract(game)
    start = next(u for u in units if u.source == "Start")
    start.target = "開始"

    adapter.inject(units, game, tmp_path / "out")
    dest = tmp_path / "out" / "game" / "tl" / "zh_hant" / "script.rpy"
    text = dest.read_text(encoding="utf-8")
    assert '    new "開始"' in text
    assert '    new "Quit"' in text


runner = CliRunner()


def _renpy_project(tmp_path, game):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.toml").write_text(
        f'''[project]
name = "tq"
source_lang = "en"
target_lang = "zh-Hant"

[engine]
type = "renpy"
game_path = "{game.as_posix()}"
sdk_path = "{(tmp_path / "sdk").as_posix()}"
''',
        encoding="utf-8",
    )
    return proj


def test_cli_extract_inject_roundtrip(tmp_path, monkeypatch):
    game = _fake_game(tmp_path)
    proj = _renpy_project(tmp_path, game)
    monkeypatch.setattr(RenpyAdapter, "_run_sdk_translate", lambda self, gp: None)

    result = runner.invoke(cli.app, ["extract", str(proj)])
    assert result.exit_code == 0, result.output
    strings_file = proj / "strings" / "script.rpy.json"
    assert strings_file.is_file()

    data = json.loads(strings_file.read_text(encoding="utf-8"))
    data["units"][0]["target"] = "你建立了新遊戲。"
    data["units"][0]["status"] = "mt"
    strings_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    result = runner.invoke(cli.app, ["inject", str(proj)])
    assert result.exit_code == 0, result.output
    dest = proj / "out" / "game" / "tl" / "zh_hant" / "script.rpy"
    assert '    e "你建立了新遊戲。"' in dest.read_text(encoding="utf-8")


def test_cli_extract_sdk_error_is_red_exit_1(tmp_path, monkeypatch):
    game = _fake_game(tmp_path)
    proj = _renpy_project(tmp_path, game)

    def boom(self, gp):
        raise RuntimeError("Ren'Py SDK translate failed")

    monkeypatch.setattr(RenpyAdapter, "_run_sdk_translate", boom)
    result = runner.invoke(cli.app, ["extract", str(proj)], catch_exceptions=False)
    assert result.exit_code == 1
    assert "translate failed" in result.output


def test_build_patch_includes_renpy_tl(tmp_path, monkeypatch):
    game = _fake_game(tmp_path)
    proj = _renpy_project(tmp_path, game)
    monkeypatch.setattr(RenpyAdapter, "_run_sdk_translate", lambda self, gp: None)
    assert runner.invoke(cli.app, ["extract", str(proj)]).exit_code == 0
    assert runner.invoke(cli.app, ["inject", str(proj)]).exit_code == 0

    stub = tmp_path / "bokucho-patcher.exe"
    stub.write_bytes(b"MZ stub")
    monkeypatch.setattr("bokucho.application.release.patcher_stub", lambda: stub)
    result = runner.invoke(cli.app, ["build-patch", str(proj)])
    assert result.exit_code == 0, result.output
    from bokucho.patcher.payload import read_patch

    patch = read_patch((proj / "dist" / "tq-en2zh-tw-patcher.exe").read_bytes())
    assert patch.mount is not None
    with zipfile.ZipFile(io.BytesIO(patch.mount)) as zf:
        assert "game/tl/zh_hant/script.rpy" in zf.namelist()
