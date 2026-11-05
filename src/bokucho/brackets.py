"""The brackets a translation keeps from what it translates."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from bokucho.findings import Finding
from bokucho.model import TextUnit
from bokucho.qa import markup_patterns, without_markup

BRACKETS = frozenset("「」（）『』")
OPENERS = frozenset("「（『")
CLOSERS = frozenset("」）』")
_CLOSER = {"「": "」", "（": "）", "『": "』"}
_SPEECH = {"『": "「", "』": "」"}
_HALF = str.maketrans("()", "（）")
_SWAPS = frozenset({("「", "（"), ("」", "）"), ("（", "「"), ("）", "」")})

_MESSAGE = {"livemaker": re.compile(r"^(pylm:text:[^:]+:\d+):")}

_CODE = {
    "speech-to-thought": "brackets_speech_to_thought",
    "thought-to-speech": "brackets_thought_to_speech",
    "mixed-swap": "brackets_mixed_swap",
    "missing-or-extra": "brackets_missing_or_extra",
    "swapped-translations": "brackets_swapped",
}


@dataclass(frozen=True, slots=True)
class BracketFinding:
    unit_ids: tuple[str, ...]
    kind: str
    source: str
    target: str
    fixes: dict[str, str] = field(default_factory=dict)
    pieces: tuple[tuple[str, str, str], ...] = ()

    @property
    def findings(self) -> list[tuple[str, Finding]]:
        """(unit id, finding) for each unit whose brackets differ, with its own brackets and fix."""
        out = []
        for unit_id, source, target in self.pieces:
            params: dict[str, str | None] = {"source": source or None, "target": target or None}
            code = _CODE[self.kind]
            if fixed := self.fixes.get(unit_id):
                code = f"{code}_fix"
                params["fixed"] = fixed
            out.append((unit_id, Finding("brackets", code, params)))
        return out


def bracket_findings(units: Sequence[TextUnit], engine: str) -> list[BracketFinding]:
    """Every message among ``units`` -- one file's -- whose brackets differ from its source's."""
    compared = []
    for message in _messages(units, engine):
        if any(not unit.target for unit in message):
            continue
        source = _marks([unit.source for unit in message], engine)
        target = _marks([unit.target for unit in message], engine)
        compared.append((message, source, target, _kind(source, target)))

    swapped: dict[int, tuple[list[str], dict[str, str]]] = {}
    for index, (a, b) in enumerate(pairwise(compared)):
        (a_units, a_source, a_target, a_kind), (b_units, b_source, b_target, b_kind) = a, b
        if (
            index not in swapped
            and a_kind is not None
            and b_kind is not None
            and len(a_units) == len(b_units) == 1
            and (a_source, b_source) == (b_target, a_target)
        ):
            exchange = {a_units[0].id: b_units[0].target, b_units[0].id: a_units[0].target}
            swapped[index] = ([b_units[0].target], exchange)
            swapped[index + 1] = ([a_units[0].target], exchange)

    findings = []
    for index, (message, source, target, kind) in enumerate(compared):
        if kind is None:
            continue
        if index in swapped:
            kind, (after, fixes) = "swapped-translations", swapped[index]
        else:
            after = _fixed(message, source, target, kind, engine)
            pairs = zip(message, after, strict=True) if after else ()
            fixes = {unit.id: text for unit, text in pairs if text != unit.target}
        findings.append(
            BracketFinding(
                unit_ids=tuple(unit.id for unit in message),
                kind=kind,
                source="".join(source),
                target="".join(target),
                fixes=fixes,
                pieces=_pieces(message, source, target, engine),
            )
        )
    return findings


def _pieces(message, source, target, engine) -> tuple[tuple[str, str, str], ...]:
    """(id, source brackets, target brackets) of each unit whose brackets differ."""
    pieces = []
    for unit in message:
        own = _marks([unit.source], engine), _marks([unit.target], engine)
        if _kind(*own) is not None:
            pieces.append((unit.id, "".join(own[0]), "".join(own[1])))
    return tuple(pieces) or ((message[0].id, "".join(source), "".join(target)),)


def _messages(units: Sequence[TextUnit], engine: str) -> list[list[TextUnit]]:
    pattern = _MESSAGE.get(engine)
    messages: dict[str, list[TextUnit]] = {}
    for unit in units:
        if unit.kind == "image" or unit.missing:
            continue
        match = pattern.match(unit.id) if pattern else None
        messages.setdefault(match.group(1) if match else unit.id, []).append(unit)
    return list(messages.values())


def _read(text: str, engine: str) -> str:
    """``text`` as the reader sees it, markup blanked to NULs in place, half-width read as full."""
    for regex in markup_patterns(engine):
        text = regex.sub(lambda match: "\0" * len(match.group()), text)
    return text.translate(_HALF)


def _found(text: str, engine: str) -> list[tuple[int, str]]:
    """(index, bracket) of each bracket outside the engine's markup, half-width read as full."""
    return [(at, mark) for at, mark in enumerate(_read(text, engine)) if mark in BRACKETS]


def _marks(texts: Sequence[str], engine: str) -> list[str]:
    return [mark for text in texts for _, mark in _found(text, engine)]


def _kind(source: list[str], target: list[str]) -> str | None:
    if [s for s in source if s not in "『』"] == [t for t in target if t not in "『』"]:
        return None
    source = [_SPEECH.get(s, s) for s in source]
    target = [_SPEECH.get(t, t) for t in target]
    if source == target:
        return None
    pairs = list(zip(source, target, strict=False))
    if len(source) == len(target) and all(s == t or (s, t) in _SWAPS for s, t in pairs):
        changed = {(s, t) for s, t in pairs if s != t}
        if changed <= {("「", "（"), ("」", "）")}:
            return "speech-to-thought"
        if changed <= {("（", "「"), ("）", "」")}:
            return "thought-to-speech"
        return "mixed-swap"
    return "missing-or-extra"


def _fixed(
    message: list[TextUnit], source: list[str], target: list[str], kind: str, engine: str
) -> list[str] | None:
    """The message's targets as the first rule that gives the source's brackets writes them."""
    if kind == "missing-or-extra" and not _pairs_up(source):
        return None
    rules = (
        (_edged, _rewrapped, _dropped, _closed, _opened)
        if kind == "missing-or-extra"
        else (_restored,)
    )
    for rule in rules:
        after = rule(message, source, target, engine)
        if after is not None and _marks(after, engine) == source:
            return after
    return None


def _restored(message, source, target, engine) -> list[str]:
    edit = {index: s for index, (s, t) in enumerate(zip(source, target, strict=True)) if s != t}
    return _edited([unit.target for unit in message], edit, engine)


def _rewrapped(message, source, target, engine) -> list[str]:
    """Each unit one pair wraps whole in the source, and none in the translation, wrapped again."""
    out = []
    for unit in message:
        pair = [mark for _, mark in _found(unit.source, engine)]
        read = without_markup(unit.source, engine).translate(_HALF).strip()
        if (
            not _found(unit.target, engine)
            and len(pair) == 2
            and pair[0] in OPENERS
            and read[:1] == pair[0]
            and read[-1:] == pair[1]
        ):
            out.append(pair[0] + unit.target + pair[1])
        else:
            out.append(unit.target)
    return out


def _dropped(message, source, target, engine) -> list[str] | None:
    """The translation without the brackets the source lacks, keeping those nearest the source's."""
    if len(target) <= len(source):
        return None
    src_at = _positions([unit.source for unit in message], engine)
    tgt_at = _positions([unit.target for unit in message], engine)
    n, m = len(target), len(source)
    cost = [[0.0] + [float("inf")] * m for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = cost[i - 1][j]
            if target[i - 1] == source[j - 1]:
                kept = cost[i - 1][j - 1] + abs(src_at[j - 1] - tgt_at[i - 1])
                cost[i][j] = min(cost[i][j], kept)
    if cost[n][m] == float("inf"):
        return None
    drop, j = set(), m
    for i in range(n, 0, -1):
        if (
            j
            and target[i - 1] == source[j - 1]
            and cost[i][j] == cost[i - 1][j - 1] + abs(src_at[j - 1] - tgt_at[i - 1])
        ):
            j -= 1
        else:
            drop.add(i - 1)
    if _holds_a_pair([target[i] for i in sorted(drop)]):
        return None
    return _edited([unit.target for unit in message], dict.fromkeys(drop, ""), engine)


def _closed(message, source, target, engine) -> list[str] | None:
    if source and target == source[:-1] and source[-1] in CLOSERS:
        return [*(unit.target for unit in message[:-1]), message[-1].target + source[-1]]
    return None


def _opened(message, source, target, engine) -> list[str] | None:
    if source and target == source[1:] and source[0] in OPENERS:
        return [source[0] + message[0].target, *(unit.target for unit in message[1:])]
    return None


def _edged(message, source, target, engine) -> list[str]:
    """Each line opened and closed as its source line is: a model closes each line it is sent."""
    out, depth = [], 0
    for unit in message:
        sources, targets = unit.source.split("\n"), unit.target.split("\n")
        aligned = len(sources) == len(targets)
        lines = []
        for index, line in enumerate(sources):
            if aligned:
                lines.append(_edged_line(line, targets[index], engine, inside=depth > 0))
            for _, mark in _found(line, engine):
                depth = depth + 1 if mark in OPENERS else max(depth - 1, 0)
        out.append("\n".join(lines) if aligned else unit.target)
    return out


_BLANK = " \t　\0"


def _edged_line(source: str, target: str, engine: str, inside: bool) -> str:
    seen, read = _read(source, engine).strip(_BLANK), _read(target, engine)
    shown = [at for at, char in enumerate(read) if char not in _BLANK]
    if not seen or not shown:
        return target
    first, last = shown[0], shown[-1]
    if (
        not inside
        and first != last
        and _CLOSER.get(read[first]) == read[last]
        and seen[0] not in OPENERS
        and seen[-1] not in CLOSERS
    ):
        return target
    chars = list(target)
    closer = seen[-1] if seen[-1] in CLOSERS else ""
    if read[last] in CLOSERS and not closer:
        chars[last] = ""
    elif closer and read[last] not in CLOSERS:
        chars[last] += closer
    opener = seen[0] if seen[0] in OPENERS else ""
    if read[first] in OPENERS and not opener:
        chars[first] = chars[first][1:]
    elif opener and read[first] not in OPENERS:
        chars[first] = opener + chars[first]
    return "".join(chars)


def _pairs_up(marks: Sequence[str]) -> bool:
    """Whether every closer closes the opener last left open, and none is left open."""
    open_ = []
    for mark in marks:
        if mark in OPENERS:
            open_.append(mark)
        elif not open_ or _CLOSER[open_.pop()] != mark:
            return False
    return not open_


def _holds_a_pair(marks: Sequence[str]) -> bool:
    """Whether an opener in ``marks`` is followed by its own closer."""
    open_ = []
    for mark in marks:
        if mark in OPENERS:
            open_.append(mark)
        elif open_ and _CLOSER[open_[-1]] == mark:
            return True
    return False


def _positions(texts: Sequence[str], engine: str) -> list[float]:
    """Where each bracket sits, as a fraction of the message read."""
    length = sum(map(len, texts)) or 1
    at, offset = [], 0
    for text in texts:
        at += [(offset + index) / length for index, _ in _found(text, engine)]
        offset += len(text)
    return at


def _edited(targets: list[str], edit: dict[int, str], engine: str) -> list[str]:
    """``targets`` with the message's n-th bracket written as ``edit[n]``."""
    out, index = [], 0
    for text in targets:
        chars = list(text)
        for at, _ in _found(text, engine):
            if index in edit:
                chars[at] = edit[index]
            index += 1
        out.append("".join(chars))
    return out
