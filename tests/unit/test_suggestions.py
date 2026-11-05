"""Tests for the suggestion engine (translation memory + glossary)."""

from bokucho.application.suggestions import (
    Suggestion,
    TranslationMemory,
    glossary_suggestions,
    suggestions_for,
)
from bokucho.model import Status, StringFile, TextUnit


def _file(*units: TextUnit) -> StringFile:
    return StringFile(source_file="a.lsb", units=list(units))


def _memory(*units: TextUnit) -> TranslationMemory:
    return TranslationMemory([_file(*units)])


def test_identical_translated_source_offers_its_target():
    memory = _memory(
        TextUnit(id="a", source="はい", target="是", status=Status.REVIEWED),
        TextUnit(id="b", source="はい"),
    )
    [suggestion] = memory.suggest("はい")
    assert suggestion.target == "是"
    assert suggestion.origin == "memory"
    assert suggestion.status is Status.REVIEWED


def test_whitespace_and_case_differences_still_match():
    memory = _memory(
        TextUnit(id="a", source="Yes Sir", target="遵命", status=Status.MT),
    )
    [suggestion] = memory.suggest("yes  sir")
    assert suggestion.target == "遵命"


def test_reviewed_ranks_above_mt():
    memory = _memory(
        TextUnit(id="a", source="おはよう", target="早1", status=Status.MT),
        TextUnit(id="b", source="おはよう", target="早2", status=Status.REVIEWED),
    )
    targets = [s.target for s in memory.suggest("おはよう")]
    assert targets == ["早2", "早1"]


def test_a_unit_never_suggests_its_own_current_target():
    memory = _memory(
        TextUnit(id="a", source="test", target="測試", status=Status.MT),
    )
    assert memory.suggest("test", exclude_target="測試") == []


def test_untranslated_and_empty_targets_are_not_suggested():
    memory = _memory(
        TextUnit(id="a", source="hello"),
        TextUnit(id="b", source="hello"),
    )
    assert memory.suggest("hello") == []


def test_identical_targets_collapse_with_a_count():
    memory = _memory(
        TextUnit(id="a", source="ありがとう", target="謝謝", status=Status.REVIEWED),
        TextUnit(id="b", source="ありがとう", target="謝謝", status=Status.REVIEWED),
        TextUnit(id="c", source="ありがとう"),
    )
    [suggestion] = memory.suggest("ありがとう")
    assert suggestion.target == "謝謝"
    assert suggestion.count == 2


def test_glossary_term_in_source_suggests_its_translation():
    suggestions = glossary_suggestions({"勇者": "hero"}, "勇者が来た")
    assert suggestions == [
        Suggestion(target="hero", origin="glossary", status=None, count=1)
    ]


def test_source_without_glossary_term_yields_no_glossary_suggestion():
    assert glossary_suggestions({"勇者": "hero"}, "こんにちは") == []


def test_suggestions_for_merges_memory_before_glossary():
    memory = _memory(
        TextUnit(id="a", source="勇者", target="英雄", status=Status.REVIEWED),
    )
    suggestions = suggestions_for(memory, {"勇者": "hero"}, "勇者", None)
    assert [s.origin for s in suggestions] == ["memory", "glossary"]
    assert [s.target for s in suggestions] == ["英雄", "hero"]
