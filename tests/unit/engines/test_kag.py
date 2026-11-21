"""KAG scenarios: which text in a .ks file is a line to translate, and writing it back."""

from __future__ import annotations

import pytest

from bokucho.engines.kag import (
    KagRules,
    ScrambledText,
    UnsafeTarget,
    decode_script,
    encode_script,
    pieces,
    rewrite,
)

SCENE = (
    "*start|はじまり\r\n"
    "[cs]\r\n"
    '[jp][name fn="春香"][endif][en][name fn=Haruka][endif]\r\n'
    "[jp]おはよう[r]ございます[endif]\r\n"
    "[en]Good morning[endif]\r\n"
    "@wait time=10\r\n"
    "; a comment\r\n"
    "[iscript]\r\nvar x = 'not text';\r\n[endscript]\r\n"
    "　地の文です。[p]\n"
    "最後の行"
)
RULES = KagRules.from_config(["en"], ["name.fn"])
NAMES = KagRules.from_config([], ["name.fn"])


def test_lines_names_and_skips():
    found = pieces(SCENE, "first.ks", RULES)
    assert [(p.id, p.kind, p.source, p.speaker) for p in found] == [
        ("name:春香", "name", "春香", None),
        ("first.ks:start:0", "dialogue", "おはよう[r]ございます", "春香"),
        ("first.ks:start:1", "dialogue", "　地の文です。", None),
        ("first.ks:start:2", "dialogue", "最後の行", None),
    ]


def test_an_unchanged_rewrite_is_byte_identical():
    sources = {p.id: p.source for p in pieces(SCENE, "first.ks", RULES)}
    data = encode_script(SCENE)
    assert encode_script(rewrite(decode_script(data), "first.ks", RULES, sources)) == data


def test_a_translation_keeps_every_other_line_and_ending():
    out = rewrite(SCENE, "first.ks", RULES, {"first.ks:start:2": "最後\n一行", "name:春香": "春香"})
    assert out.splitlines(keepends=True)[:-1] == SCENE.splitlines(keepends=True)[:-1]
    assert out.endswith("最後[r]一行")


def test_the_name_unit_changes_only_the_attribute():
    text = '[name fn="春香"]\n春香です\n'
    out = rewrite(text, "a.ks", NAMES, {"name:春香": "Haruka"})
    assert out == '[name fn="Haruka"]\n春香です\n'


def test_a_skipped_branch_crosses_lines_and_nests():
    text = "[en]\nA\n[if exp='1']B[endif]\nC\n[endif]\nD\n"
    assert [p.source for p in pieces(text, "a.ks", KagRules.from_config(["en"], []))] == ["D"]


def test_quotes_brackets_and_escapes():
    text = '[eval exp="a[0]"]x[[y[l]\n'
    assert [p.source for p in pieces(text, "a.ks", KagRules())] == ["x[[y"]


def test_a_macro_defined_on_one_line_ends_on_that_line():
    text = '[macro name=jp][if exp="1"][endmacro]\nA\n@macro name=en\nB\n@endmacro\nC\n'
    assert [p.source for p in pieces(text, "a.ks", KagRules())] == ["A", "C"]


def test_command_lines_are_tags():
    text = "@name fn=春香\n春香です\n@en\nHello\n@endif\n"
    found = pieces(text, "a.ks", KagRules.from_config(["en"], ["name.fn"]))
    assert [(p.source, p.speaker) for p in found] == [("春香", None), ("春香です", "春香")]


def test_text_under_a_skipped_label_is_left_until_the_next_label():
    text = "*a_jp\nA\n*a_CHH|title\nB\n[name fn=春香]C\n*b\nD\n"
    rules = KagRules.from_config([], ["name.fn"], ["*_chh"])
    assert [(p.id, p.source) for p in pieces(text, "a.ks", rules)] == [
        ("a.ks:a_jp:0", "A"),
        ("a.ks:b:0", "D"),
    ]


def test_ids_hold_when_a_line_is_added_under_another_label():
    before = "*a\nA\n*b\nB\n"
    after = "*a\nA\nA2\n*b\nB\n"

    def ids(text):
        return {p.source: p.id for p in pieces(text, "a.ks", KagRules())}

    assert ids(before)["B"] == ids(after)["B"] == "a.ks:b:0"


@pytest.mark.parametrize("target", ["早[安", "早安[font size=9]"])
def test_a_bracket_that_opens_no_source_tag_is_refused(target):
    with pytest.raises(UnsafeTarget, match="a.ks::0"):
        rewrite("おはよう[l]\n", "a.ks", KagRules(), {"a.ks::0": target})


@pytest.mark.parametrize(
    ("source", "target"),
    [("おは[[よう\n", "早安["), ("おは[r]よう\n", "早安[r")],
)
def test_an_unclosed_bracket_is_refused_even_when_the_source_holds_its_text(source, target):
    with pytest.raises(UnsafeTarget, match="a.ks::0"):
        rewrite(source, "a.ks", KagRules(), {"a.ks::0": target})


def test_brackets_the_game_reads_are_allowed():
    out = rewrite("おは[l]よう\n", "a.ks", KagRules(), {"a.ks::0": "早[[安[r]好[l]"})
    assert out == "早[[安[r]好[l]\n"


@pytest.mark.parametrize("target", ["*嘆氣*", " @_@", ";)"])
def test_a_line_that_would_turn_into_a_label_command_or_comment_is_refused(target):
    with pytest.raises(UnsafeTarget, match="a.ks::0"):
        rewrite("おはよう\n", "a.ks", KagRules(), {"a.ks::0": target})


def test_a_line_after_a_tag_may_start_with_any_character():
    out = rewrite("[cm]おはよう\n", "a.ks", KagRules(), {"a.ks::0": "*嘆氣*"})
    assert out == "[cm]*嘆氣*\n"


@pytest.mark.parametrize("target", ['春"香', "春\n香"])
def test_a_name_that_cannot_be_quoted_is_refused(target):
    with pytest.raises(UnsafeTarget, match="name:春香"):
        rewrite("[name fn=春香]\n", "a.ks", NAMES, {"name:春香": target})


@pytest.mark.parametrize("target", ["", "   ", "　"])
def test_a_blank_target_leaves_the_line(target):
    assert rewrite("おはよう\n", "a.ks", KagRules(), {"a.ks::0": target}) == "おはよう\n"


@pytest.mark.parametrize(
    "data", [b"\xfe\xfe\x01abc", b"\x00\x14\x15abc", "ok".encode("cp932") + b"\x07"]
)
def test_scrambled_scripts_are_refused(data):
    with pytest.raises(ScrambledText):
        decode_script(data)


@pytest.mark.parametrize(
    "data,text",
    [
        (b"\xff\xfe" + "あ".encode("utf-16-le"), "あ"),
        (b"\xef\xbb\xbf" + "あ".encode(), "あ"),
        ("あ".encode(), "あ"),
        ("あ".encode("cp932"), "あ"),
    ],
)
def test_scripts_decode(data, text):
    assert decode_script(data) == text


def test_scripts_are_written_as_utf16_with_a_bom():
    assert encode_script("あ") == b"\xff\xfe" + "あ".encode("utf-16-le")


@pytest.mark.parametrize("entry", ["name", "name.fn.x", ".fn"])
def test_a_name_attribute_must_name_a_tag_and_an_attribute(entry):
    with pytest.raises(ValueError, match="kag_name_attributes"):
        KagRules.from_config([], [entry])
