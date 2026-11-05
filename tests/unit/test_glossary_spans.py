"""Tests for the glossary span matcher powering the browse-list underline."""

from bokucho.application.glossary import glossary_spans


def test_repeated_term_yields_every_occurrence():
    assert glossary_spans({"王": "king"}, "王と王") == [(0, 1), (2, 3)]


def test_adjacent_terms_both_match():
    spans = glossary_spans({"勇者": "hero", "魔王": "demon lord"}, "勇者魔王")
    assert spans == [(0, 2), (2, 4)]
