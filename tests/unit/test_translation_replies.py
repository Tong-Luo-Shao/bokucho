"""Reading the translations out of a model's finished reply."""

from __future__ import annotations

import pytest

from bokucho.translators.base import TranslationResponseError, parse_translations


@pytest.mark.parametrize(
    "reply",
    [
        'Here are the translations:\n```json\n["甲", "乙"]\n```',
        '<think>The user wants [two] lines, so I answer with two.</think>\n["甲", "乙"]',
        '["甲", "乙"]\nLet me know if any line needs another wording.',
        '```json\n["甲", "乙"]\n```',
        '<think>A first draft: ["x", "y"]</think>\n["甲", "乙"]',
        'The source was ["a", "b"].\n```json\n["甲", "乙"]\n```',
        'Sure, ["甲", "乙"]',
    ],
)
def test_the_array_is_read_past_what_the_model_says_around_it(reply: str) -> None:
    assert parse_translations(reply, 2) == ["甲", "乙"]


@pytest.mark.parametrize(
    "reply",
    [
        '[["那一天，我", "遇見了你"] ["再見"]]',
        '[["那一天，我", "遇見了你"], ["再',
        '[["再見"] ["那一天，我", "遇見了你"]]',
        '[["再見"], "a ] b" ["那一天，我", "遇見了你"]]',
    ],
)
def test_a_broken_reply_is_not_read_from_inside_one_of_its_entries(reply: str) -> None:
    with pytest.raises(TranslationResponseError):
        parse_translations(reply, 2, [2, 1])
