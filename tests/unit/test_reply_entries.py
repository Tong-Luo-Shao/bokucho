"""Reading the answers out of a JSON array that has not finished arriving."""

from __future__ import annotations

import pytest

from bokucho.translators.base import ReplyEntries


def _read(*pieces: str) -> ReplyEntries:
    entries = ReplyEntries()
    for piece in pieces:
        entries.feed(piece)
    return entries


def test_finished_strings_are_entries_and_the_one_being_written_is_partial() -> None:
    entries = _read('["很好", "再')

    assert entries.done == ["很好"]
    assert entries.partial == "再"


def test_between_entries_nothing_is_partial() -> None:
    entries = _read('["很好",')

    assert entries.done == ["很好"]
    assert entries.partial is None


def test_an_escape_split_across_pieces_is_read_whole() -> None:
    entries = _read('["a\\', 'nb", "\\u59', '7d"]')

    assert entries.done == ["a\nb", "好"]


def test_a_character_outside_the_basic_plane_is_one_character() -> None:
    assert _read('["\\ud83d\\ude00"]').done == ["\U0001f600"]


def test_text_before_the_array_is_not_read() -> None:
    assert _read('```json\n["很好"]\n```').done == ["很好"]


def test_an_entry_of_lines_is_its_lines_joined() -> None:
    entries = _read('[["一", "二"], ["三"')

    assert entries.done == ["一\n二"]
    assert entries.partial == "三"


@pytest.mark.parametrize(
    "reply",
    ['[{"lines": ["一", "二"]}]', '[{"translation": "一\\n二"}]'],
)
def test_an_entry_as_an_object_is_its_text_without_the_keys(reply: str) -> None:
    assert _read(reply).done == ["一\n二"]


def test_nothing_after_the_array_is_read() -> None:
    assert _read('["一"] and ["二"]').done == ["一"]
