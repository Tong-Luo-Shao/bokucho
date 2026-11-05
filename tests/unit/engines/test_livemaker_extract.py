"""What extraction makes of a block whose name is not a plain string."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bokucho.engines.livemaker import LiveMakerAdapter

pytest.importorskip("livemaker")


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text
        self.name_label = None


class _Expression:
    """What pylivemaker hands back when a block's name is computed."""

    def __str__(self) -> str:
        return '"ステータスボックス"'


class _Identifier:
    line_no = 108
    block_index = 0

    def __init__(self, name: object) -> None:
        self.name = name

    def __str__(self) -> str:
        return "pylm:text:00000CBB.lsb:108:0"


class _Script:
    commands = ()

    def __init__(self, name: object) -> None:
        self._name = name

    def get_text_blocks(self):
        return [(_Identifier(self._name), _Block("げんき"))]

    def text_scenarios(self, run_order: bool = True):
        return []


def _adapter(monkeypatch, name: object) -> LiveMakerAdapter:
    adapter = LiveMakerAdapter()
    monkeypatch.setattr(adapter, "_open", lambda path: _Script(name))
    return adapter


def _game(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    root.mkdir()
    (root / "00000CBB.lsb").write_bytes(b"stub")
    return root


def test_a_computed_scene_name_is_read_as_the_expression_it_prints_as(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [unit] = _adapter(monkeypatch, _Expression()).extract(_game(tmp_path))

    assert unit.context.scene == '"ステータスボックス"'


def test_an_engine_reference_survives_being_written_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [unit] = _adapter(monkeypatch, _Expression()).extract(_game(tmp_path))

    assert json.loads(json.dumps(unit.engine_ref))["name"] == '"ステータスボックス"'


def test_a_block_with_no_name_still_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [unit] = _adapter(monkeypatch, "").extract(_game(tmp_path))

    assert unit.context.scene is None


class _TpWord:
    """A scenario body built from real glyphs, split by pylivemaker itself."""

    def __init__(self, body: list) -> None:
        self.body = body

    def get_text_blocks(self):
        from livemaker.lsb.novel import LNSText

        return LNSText.from_tpword(self)


class _LineScript:
    """One scenario line, numbered the way pylivemaker numbers it."""

    call_name = "00002510.lsb"
    commands = ()

    def __init__(self, body: list) -> None:
        self._tpword = _TpWord(body)

    def text_scenarios(self, run_order: bool = True):
        return [(39, "", self._tpword)]

    def get_text_blocks(self):
        from livemaker.lsb import LMScript

        return LMScript.get_text_blocks(self)


class _Proxy:
    def decode(self, text: str) -> str:
        return text.replace("X", "貓")


def _chars(text: str) -> list:
    from livemaker.lsb.novel import TWdChar

    return [TWdChar(ch=ch) for ch in text]


def _var(name: str):
    from livemaker.lsb.novel import TWdOpeVar

    return TWdOpeVar(var_name=name)


def _page():
    from livemaker.lsb.novel import BreakType, TWdOpeReturn

    return TWdOpeReturn(break_type=BreakType.PAGE)


def _extract_line(monkeypatch, tmp_path: Path, body: list, proxy=None):
    adapter = LiveMakerAdapter()
    adapter.proxy = proxy
    monkeypatch.setattr(adapter, "_open", lambda path: _LineScript(body))
    root = tmp_path / "game"
    root.mkdir()
    (root / "00002510.lsb").write_bytes(b"stub")
    return {unit.id: unit for unit in adapter.extract(root)}


def test_a_sentence_cut_by_variables_is_extracted_as_one_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = [
        *_chars("　"),
        _var("【品名】"),
        *_chars(" を"),
        _var("【個数】"),
        *_chars("個、手に入れた。"),
        _page(),
    ]

    units = _extract_line(monkeypatch, tmp_path, body)

    assert list(units) == ["pylm:text:00002510.lsb:39:0-2"]
    unit = units["pylm:text:00002510.lsb:39:0-2"]
    assert unit.source == "　<VAR 【品名】> を<VAR 【個数】>個、手に入れた。"
    assert unit.engine_ref["line_no"] == 39
    assert unit.engine_ref["run"] == 0
    assert unit.engine_ref["block_id"] == "pylm:text:00002510.lsb:39:0-2"
    assert unit.engine_ref["parts"][0] == {"block": "pylm:text:00002510.lsb:39:0"}
    assert unit.engine_ref["parts"][1] == {"var": "【品名】"}
    assert json.loads(json.dumps(unit.engine_ref)) == unit.engine_ref


def test_blocks_outside_a_run_keep_their_own_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = [
        *_chars("A"), _var("x"), *_chars("B"), _page(),
        *_chars("さようなら。"), _page(),
    ]

    units = _extract_line(monkeypatch, tmp_path, body)

    assert list(units) == [
        "pylm:text:00002510.lsb:39:0-1",
        "pylm:text:00002510.lsb:39:2",
    ]
    plain = units["pylm:text:00002510.lsb:39:2"]
    assert plain.source == "さようなら。"
    assert "parts" not in plain.engine_ref


def test_a_proxy_map_decodes_the_text_of_a_merged_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = [*_chars("X"), _var("nX"), _page()]

    [unit] = _extract_line(monkeypatch, tmp_path, body, proxy=_Proxy()).values()

    assert unit.source == "貓<VAR nX>"
