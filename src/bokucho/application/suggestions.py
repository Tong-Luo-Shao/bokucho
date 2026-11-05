"""Suggestion engine — translation memory and glossary hits."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from bokucho.model import Status, StringFile

_STATUS_RANK = {Status.REVIEWED: 0, Status.MT: 1}


@dataclass(frozen=True, slots=True)
class Suggestion:
    target: str
    origin: str
    status: Status | None
    count: int


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


class TranslationMemory:
    """Index of translated units: normalized source -> [(target, status)]."""

    def __init__(self, string_files: Sequence[StringFile]) -> None:
        index: dict[str, list[tuple[str, Status]]] = {}
        for string_file in string_files:
            for unit in string_file.units:
                if not unit.target or unit.status is Status.UNTRANSLATED:
                    continue
                index.setdefault(_normalize(unit.source), []).append(
                    (unit.target, unit.status)
                )
        self._index = index

    def suggest(
        self, source: str, exclude_target: str | None = None
    ) -> list[Suggestion]:
        matches = self._index.get(_normalize(source), [])
        best: dict[str, tuple[Status, int]] = {}
        for target, status in matches:
            if target == exclude_target:
                continue
            seen = best.get(target)
            if seen is None:
                best[target] = (status, 1)
            else:
                top = min(status, seen[0], key=lambda s: _STATUS_RANK.get(s, 99))
                best[target] = (top, seen[1] + 1)
        suggestions = [
            Suggestion(target=target, origin="memory", status=status, count=count)
            for target, (status, count) in best.items()
        ]
        suggestions.sort(key=lambda s: (_STATUS_RANK.get(s.status, 99), -s.count))
        return suggestions


def glossary_suggestions(mapping: dict[str, str], source: str) -> list[Suggestion]:
    """One suggestion per glossary term found in ``source``."""
    return [
        Suggestion(target=target, origin="glossary", status=None, count=1)
        for term, target in mapping.items()
        if term and term in source
    ]


def suggestions_for(
    memory: TranslationMemory,
    mapping: dict[str, str],
    source: str,
    exclude_target: str | None = None,
) -> list[Suggestion]:
    """Memory suggestions first, then glossary suggestions."""
    return memory.suggest(source, exclude_target) + glossary_suggestions(
        mapping, source
    )
