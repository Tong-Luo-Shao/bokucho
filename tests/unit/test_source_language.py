"""Which lines a project reads as its source language."""

import pytest

from bokucho.language import matches_source_language, source_language_index


@pytest.mark.parametrize("line,source_lang,expected", [
    # Japanese written in kanji alone -- menus, items, skills -- is Japanese.
    ("戦闘表示設定", "ja", True),
    ("装備", "ja", True),
    ("ＨＰ回復！", "ja", True),
    ("魔物C", "ja", True),
    ("干渉", "ja", True),
    # The middle dot and the long mark are punctuation, not a reason to doubt it.
    ("剣・槍・斧", "ja", True),
    ("攻撃力・防御力上昇", "ja", True),
    # A single old form is one Japanese still writes.
    ("怒濤", "ja", True),
    # A form only Chinese writes still marks the line Chinese.
    ("战斗显示设置", "ja", False),
    ("戰鬥顯示設定", "ja", False),
    ("今天的天氣讓人很舒服", "ja", False),
    # A Chinese project keeps reading its own lines as Chinese.
    ("設定", "zh-tw", True),
])
def test_kanji_alone_is_read_by_the_project_source(line, source_lang, expected):
    assert matches_source_language(line, source_lang) is expected


@pytest.mark.parametrize("line,source_lang,expected", [
    # A line Simplified and Traditional write alike belongs to either region.
    ("你好朋友", "zh-tw", True),
    ("你好朋友", "zh-cn", True),
    ("今天天气很好", "zh-tw", False),
    # Short words in a Latin-script project are told by their script.
    ("OK", "en", True),
    ("Options", "en", True),
    ("Quit", "en", True),
    ("OK", "ja", False),
])
def test_ordinary_lines_of_a_chinese_or_english_project_are_its_own(
    line, source_lang, expected
):
    assert matches_source_language(line, source_lang) is expected


def test_a_japanese_projects_kanji_lines_are_indexed_as_japanese():
    languages, index = source_language_index(
        [(("a.lsb", "1"), "戦闘表示設定"), (("a.lsb", "2"), "装備")], "ja"
    )
    assert languages == ["ja"]
    assert index[("a.lsb", "2")] == {"ja"}
