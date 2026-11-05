"""Finding the sentences pylivemaker cut wherever a variable is inserted."""

from __future__ import annotations

import pytest

pytest.importorskip("livemaker")

from livemaker.lsb.novel import (  # noqa: E402
    BreakType,
    LNSText,
    TWdChar,
    TWdOpeReturn,
    TWdOpeVar,
    TWdType,
)

from bokucho.engines.livemaker_runs import (  # noqa: E402
    VariableMismatch,
    find_runs,
    render_run,
    run_source,
    splice_runs,
)


class _TpWord:
    def __init__(self, body: list) -> None:
        self.body = body


def _chars(text: str) -> list:
    return [TWdChar(ch=ch) for ch in text]


def _var(name: str) -> TWdOpeVar:
    return TWdOpeVar(var_name=name)


def _line() -> TWdOpeReturn:
    return TWdOpeReturn(break_type=BreakType.LINE)


def _page() -> TWdOpeReturn:
    return TWdOpeReturn(break_type=BreakType.PAGE)


def _runs(body: list):
    return find_runs(body, LNSText.from_tpword(_TpWord(body)))


def test_a_sentence_cut_by_two_variables_is_one_run() -> None:
    body = [
        *_chars("　"),
        _var("【品名】"),
        *_chars(" を"),
        _var("【個数】"),
        *_chars("個、手に入れた。"),
        _page(),
    ]

    [run] = _runs(body)

    assert (run.first_block, run.last_block) == (0, 2)
    assert run_source(body, run, str) == "　<VAR 【品名】> を<VAR 【個数】>個、手に入れた。"
    assert run.parts == (
        {"block": 0},
        {"var": "【品名】"},
        {"block": 1},
        {"var": "【個数】"},
        {"block": 2},
    )


def test_a_leading_variable_belongs_to_the_run() -> None:
    body = [_var("【名前】"), *_chars("は笑った。"), _page()]

    [run] = _runs(body)

    assert run_source(body, run, str) == "<VAR 【名前】>は笑った。"
    assert run.parts == ({"var": "【名前】"}, {"block": 0})
    assert (run.first_block, run.last_block) == (0, 0)


def test_a_page_break_ends_a_run() -> None:
    body = [
        *_chars("A"), _var("x"), *_chars("B"), _page(),
        *_chars("C"), _var("y"), *_chars("D"), _page(),
    ]

    runs = _runs(body)

    assert [(run.first_block, run.last_block) for run in runs] == [(0, 1), (2, 3)]
    assert [run_source(body, run, str) for run in runs] == ["A<VAR x>B", "C<VAR y>D"]


def test_a_block_with_no_variable_is_not_a_run() -> None:
    assert _runs([*_chars("さようなら。"), _page()]) == []


def test_trailing_line_breaks_stay_outside_the_run() -> None:
    body = [*_chars("A"), _var("x"), *_chars("B"), _line(), _line(), _page()]

    [run] = _runs(body)

    assert run.end == 3
    assert run_source(body, run, str) == "A<VAR x>B"


def test_a_line_break_between_a_block_and_a_variable_is_a_gap() -> None:
    body = [*_chars("A"), _line(), _var("x"), *_chars("B"), _page()]

    [run] = _runs(body)

    assert run_source(body, run, str) == "A\n<VAR x>B"
    assert run.parts == ({"block": 0}, {"text": "\n"}, {"var": "x"}, {"block": 1})


def test_a_name_that_cannot_be_a_token_is_not_merged() -> None:
    assert _runs([*_chars("A"), _var("a>b"), *_chars("B"), _page()]) == []


def test_decode_applies_to_text_but_not_to_names() -> None:
    body = [*_chars("a"), _var("name"), *_chars("b"), _page()]

    [run] = _runs(body)

    assert run_source(body, run, str.upper) == "A<VAR name>B"


def _sentence() -> list:
    return [
        *_chars("　"),
        _var("【品名】"),
        *_chars(" を"),
        _var("【個数】"),
        *_chars("個、手に入れた。"),
        _page(),
    ]


def _variables(glyphs: list) -> list:
    return [glyph for glyph in glyphs if glyph.type == TWdType.TWdOpeVar]


def _text(glyphs: list) -> str:
    return "".join(
        str(glyph.ch) if glyph.type == TWdType.TWdChar else "\n"
        for glyph in glyphs
        if glyph.type != TWdType.TWdOpeVar
    )


def test_a_moved_variable_keeps_its_original_glyph() -> None:
    body = _sentence()
    [run] = _runs(body)

    glyphs = render_run(body, run, "得到了<VAR 【個数】>個<VAR 【品名】>。", str)

    moved = _variables(glyphs)
    assert [glyph.name for glyph in moved] == ["【個数】", "【品名】"]
    assert moved[0] is body[4]
    assert moved[1] is body[1]
    assert _text(glyphs) == "得到了個。"


def test_a_variable_can_open_or_close_the_sentence() -> None:
    body = _sentence()
    [run] = _runs(body)

    glyphs = render_run(body, run, "<VAR 【品名】>得到了<VAR 【個数】>", str)

    assert glyphs[0] is body[1]
    assert glyphs[-1] is body[4]


def test_a_repeated_name_takes_the_glyphs_in_source_order() -> None:
    body = [*_chars("A"), _var("n"), *_chars("B"), _var("n"), *_chars("C"), _page()]
    [run] = _runs(body)

    glyphs = render_run(body, run, "<VAR n>x<VAR n>", str)

    assert _variables(glyphs) == [body[1], body[3]]


def test_new_characters_carry_the_first_characters_style() -> None:
    body = [
        TWdChar(ch="A", decorator=3, text_speed=7),
        _var("n"),
        TWdChar(ch="B", decorator=5),
        _page(),
    ]
    [run] = _runs(body)

    glyphs = render_run(body, run, "xy<VAR n>", str)

    chars = [glyph for glyph in glyphs if glyph.type == TWdType.TWdChar]
    assert [(glyph.ch, glyph.decorator, glyph.text_speed) for glyph in chars] == [
        ("x", 3, 7),
        ("y", 3, 7),
    ]


def test_a_newline_becomes_a_line_break() -> None:
    body = _sentence()
    [run] = _runs(body)

    glyphs = render_run(body, run, "得\n到<VAR 【品名】><VAR 【個数】>", str)

    assert glyphs[1].type == TWdType.TWdOpeReturn
    assert glyphs[1].break_type == BreakType.LINE


def test_a_dropped_variable_is_refused() -> None:
    body = _sentence()
    [run] = _runs(body)

    with pytest.raises(VariableMismatch):
        render_run(body, run, "得到了<VAR 【品名】>。", str)


def test_an_invented_variable_is_refused() -> None:
    body = _sentence()
    [run] = _runs(body)

    with pytest.raises(VariableMismatch):
        render_run(body, run, "<VAR 【品名】><VAR 【個数】><VAR 【他】>", str)


def test_encode_applies_to_text_but_not_to_names() -> None:
    body = [*_chars("a"), _var("name"), _page()]
    [run] = _runs(body)

    glyphs = render_run(body, run, "a<VAR name>", str.upper)

    assert glyphs[0].ch == "A"
    assert glyphs[1] is body[1]


def test_splicing_replaces_each_run_where_it_stood() -> None:
    body = [
        *_chars("A"), _var("x"), *_chars("B"), _page(),
        *_chars("C"), _var("y"), *_chars("D"), _page(),
    ]
    first, second = _runs(body)

    spliced = splice_runs(
        body,
        [
            (first, render_run(body, first, "<VAR x>long", str)),
            (second, render_run(body, second, "z<VAR y>", str)),
        ],
    )

    assert [run_source(spliced, run, str) for run in _runs(spliced)] == [
        "<VAR x>long",
        "z<VAR y>",
    ]
