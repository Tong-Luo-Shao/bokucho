from __future__ import annotations

import re

import pytest

from bokucho.layout import reflow, split_layout

_VARIABLE = re.compile(r"%[^%\s]+%")


@pytest.mark.parametrize(
    ("source", "translated", "expected"),
    [
        ("　　【アオイ】\n　　　こんにちは。", "【阿奧伊】你好。", "　　【阿奧伊】\n　　　你好。"),
        ("　こんにちは、アオイ。", "你好，\n阿奧伊。", "　你好，阿奧伊。"),
        ("　あいうえお\n　かきくけこ", "甲乙丙丁戊己庚辛", "　甲乙丙丁\n　戊己庚辛"),
        ("　あいう\n　えおか", "甲乙。」丙丁", "　甲乙。」\n　丙丁"),
        ("　あいうえお\n　かきくけこ", "Hello brave new world", "　Hello brave\n　new world"),
        ("　こんにちは", "Hello\nworld", "　Hello world"),
        ("　あ\n\n　い", "甲\n\n乙", "　甲\n\n　乙"),
    ],
)
def test_reflow_lays_a_translation_out_in_the_source_lines(
    source: str, translated: str, expected: str
) -> None:
    layout, region = split_layout(source)
    assert reflow(layout, translated, region) == expected


def test_reflow_never_breaks_inside_markup() -> None:
    layout, region = split_layout("　あいう\n　えおか")

    assert reflow(layout, "%名前%甲乙", region, (_VARIABLE,)) == "　%名前%\n　甲乙"


def test_reflow_gives_up_when_there_are_fewer_characters_than_lines() -> None:
    layout, region = split_layout("　あ\n　い\n　う")

    assert reflow(layout, "好", region) is None
