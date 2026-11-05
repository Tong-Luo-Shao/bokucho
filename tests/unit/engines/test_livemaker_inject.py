"""Writing a sentence cut by variables back into its scenario line."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("livemaker")

from livemaker.lsb import LMScript  # noqa: E402
from livemaker.lsb.command import CommandType  # noqa: E402
from livemaker.lsb.novel import (  # noqa: E402
    BreakType,
    LNSText,
    TpWord,
    TWdChar,
    TWdOpeReturn,
    TWdOpeVar,
    TWdType,
)

from bokucho.engines.livemaker import LiveMakerAdapter  # noqa: E402
from bokucho.engines.livemaker_runs import find_runs, run_source  # noqa: E402


class _TpWord:
    """A scenario body that pylivemaker's own block code can read and replace."""

    replace_text_blocks = TpWord.replace_text_blocks

    def __init__(self, body: list) -> None:
        self.body = body

    def get_text_blocks(self):
        return LNSText.from_tpword(self)

    def replace_body(self, body, ruby_text=None) -> None:
        self.body = list(body)


class _Command:
    type = CommandType.TextIns

    def __init__(self, tpword: _TpWord) -> None:
        self._tpword = tpword

    def get(self, key, default=None):
        return self._tpword if key == "Text" else default


class _Script:
    """One scenario line, with pylivemaker's own extract and replace logic."""

    call_name = "00002510.lsb"
    commands = ()
    get_text_blocks = LMScript.get_text_blocks
    replace_text = LMScript.replace_text
    replace_text_blocks = LMScript.replace_text_blocks

    def __init__(self, body: list) -> None:
        self.tpword = _TpWord(body)

    def text_scenarios(self, run_order: bool = True):
        return [(39, "", self.tpword)]

    def get_command(self, line_no: int):
        if line_no != 39:
            raise KeyError(line_no)
        return 0, _Command(self.tpword)


def _chars(text: str) -> list:
    return [TWdChar(ch=ch) for ch in text]


def _var(name: str) -> TWdOpeVar:
    return TWdOpeVar(var_name=name)


def _page() -> TWdOpeReturn:
    return TWdOpeReturn(break_type=BreakType.PAGE)


def _game(tmp_path: Path) -> Path:
    root = tmp_path / "game"
    root.mkdir()
    (root / "00002510.lsb").write_bytes(b"stub")
    return root


def _adapter(monkeypatch, script: _Script) -> LiveMakerAdapter:
    adapter = LiveMakerAdapter()
    monkeypatch.setattr(adapter, "_open", lambda path: script)
    monkeypatch.setattr(adapter, "_serialize_validated", lambda lsb: b"")
    return adapter


def _units(monkeypatch, tmp_path: Path, body: list) -> dict:
    root = tmp_path / "extracted"
    root.mkdir()
    (root / "00002510.lsb").write_bytes(b"stub")
    adapter = _adapter(monkeypatch, _Script(body))
    return {unit.id: unit for unit in adapter.extract(root)}


def _sources(body: list) -> list[str]:
    runs = find_runs(body, LNSText.from_tpword(_TpWord(body)))
    return [run_source(body, run, str) for run in runs]


def _sentence() -> list:
    return [
        *_chars("　"),
        _var("【品名】"),
        *_chars(" を"),
        _var("【個数】"),
        *_chars("個、手に入れた。"),
        _page(),
    ]


def test_a_translated_sentence_is_written_with_its_variables_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [unit] = _units(monkeypatch, tmp_path, _sentence()).values()
    unit.target = "得到了<VAR 【個数】>個<VAR 【品名】>。"
    body = _sentence()
    script = _Script(body)

    _adapter(monkeypatch, script).inject([unit], _game(tmp_path), tmp_path / "out")

    written = script.tpword.body
    assert _sources(written) == ["得到了<VAR 【個数】>個<VAR 【品名】>。"]
    variables = [glyph for glyph in written if glyph.type == TWdType.TWdOpeVar]
    assert variables == [body[4], body[1]]


def test_only_the_translated_run_of_two_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def body() -> list:
        return [
            *_chars("A"), _var("x"), *_chars("B"), _page(),
            *_chars("さようなら。"), _page(),
            *_chars("C"), _var("y"), *_chars("D"), _page(),
        ]

    units = _units(monkeypatch, tmp_path, body())
    units["pylm:text:00002510.lsb:39:2"].target = "さようなら、みなさん。"
    units["pylm:text:00002510.lsb:39:3-4"].target = "z<VAR y>"
    script = _Script(body())

    _adapter(monkeypatch, script).inject(list(units.values()), _game(tmp_path), tmp_path / "out")

    written = script.tpword.body
    assert _sources(written) == ["A<VAR x>B", "z<VAR y>"]
    blocks = LNSText.from_tpword(script.tpword)
    assert blocks[2].text == "さようなら、みなさん。"


def test_a_changed_game_line_stops_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [unit] = _units(monkeypatch, tmp_path, _sentence()).values()
    unit.target = "得到了<VAR 【個数】>個<VAR 【品名】>。"
    changed = [*_chars("　"), _var("【品名】"), *_chars("が"), _var("【個数】"), _page()]

    with pytest.raises(ValueError, match="pylm:text:00002510.lsb:39:0-2"):
        _adapter(monkeypatch, _Script(changed)).inject(
            [unit], _game(tmp_path), tmp_path / "out"
        )


def test_a_variable_mismatch_names_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [unit] = _units(monkeypatch, tmp_path, _sentence()).values()
    unit.target = "得到了<VAR 【品名】>。"

    with pytest.raises(ValueError, match="pylm:text:00002510.lsb:39:0-2.*【個数】"):
        _adapter(monkeypatch, _Script(_sentence())).inject(
            [unit], _game(tmp_path), tmp_path / "out"
        )


def test_an_unchanged_script_is_copied_after_validation(tmp_path: Path, monkeypatch) -> None:
    from livemaker.exceptions import LiveMakerException

    from bokucho.model import TextUnit

    root = tmp_path / "game"
    root.mkdir()
    source = root / "empty.lsb"
    original = LMScript(commands=[]).to_lsb()
    source.write_bytes(original)
    adapter = LiveMakerAdapter()

    def unexpected_rebuild(script):
        raise AssertionError("An unchanged script needs no rebuild.")

    monkeypatch.setattr(adapter, "_serialize_validated", unexpected_rebuild)
    units = [TextUnit(id="u1", source="original", engine_ref={"lsb": source.name})]
    adapter.inject(units, root, tmp_path / "out")
    assert (tmp_path / "out" / source.name).read_bytes() == original
    adapter.inject(units, root, root)
    assert source.read_bytes() == original

    source.write_bytes(b"not a script")
    with pytest.raises(LiveMakerException):
        adapter.inject(units, root, tmp_path / "out")
