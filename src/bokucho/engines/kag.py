"""KAG scenarios (.ks): which text is a line to translate, and writing translations back.

Units never span physical lines, so a rewritten file keeps its line count and every line
ending; ``rewrite`` re-runs ``pieces`` so extract and inject cannot disagree on positions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

NAME_PREFIX = "name:"
_SPACE = " \t"
_BOM_UTF16 = b"\xff\xfe"
_BOM_UTF8 = b"\xef\xbb\xbf"
# KiriKiri's own text scrambling starts a file with FE FE and a mode byte.
_SCRAMBLED = b"\xfe\xfe"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85  ]")
# What a KAG line means when it starts (after spaces) with one of these.
_LINE_START = {";": "comment", "*": "label", "@": "command"}


class ScrambledText(ValueError):
    """The script is encrypted or not text at all."""


class UnsafeTarget(ValueError):
    """A translation would change what the game reads besides the text (message: unit id first)."""


@dataclass(frozen=True, slots=True)
class KagRules:
    """Project settings: what opens another language's text, and speaker-name attributes.

    A skipped branch runs from its tag to the matching ``[endif]``; a skipped label (a
    glob on the label name, e.g. ``*_chh``) runs from that label to the next one.
    """

    skip_branches: frozenset[str] = frozenset()
    name_attributes: frozenset[tuple[str, str]] = frozenset()
    skip_labels: tuple[str, ...] = ()

    @classmethod
    def from_config(
        cls,
        skip_branches: Sequence[str] | None,
        name_attributes: Sequence[str] | None,
        skip_labels: Sequence[str] | None = None,
    ) -> KagRules:
        names = set()
        for entry in name_attributes or ():
            tag, dot, attribute = entry.strip().lower().partition(".")
            if not tag or not dot or not attribute or "." in attribute:
                raise ValueError(
                    f'kag_name_attributes entry {entry!r} must look like "tag.attribute".'
                )
            names.add((tag, attribute))
        return cls(
            frozenset(s.strip().lower() for s in skip_branches or ()),
            frozenset(names),
            tuple(s.strip().casefold() for s in skip_labels or ()),
        )

    def skips_label(self, label: str) -> bool:
        return any(fnmatchcase(label.casefold(), pattern) for pattern in self.skip_labels)


@dataclass(frozen=True, slots=True)
class Piece:
    """A unit's place in a script; ``start``/``end`` index the line without its ending."""

    id: str
    kind: str
    source: str
    speaker: str | None
    line: int
    start: int
    end: int


@dataclass(slots=True)
class _Tag:
    name: str
    end: int
    # attribute -> (value, start, end); the span includes the value's quotes.
    values: dict[str, tuple[str, int, int]] = field(default_factory=dict)


def _bare_end(body: str, at: int, command: bool) -> int:
    while at < len(body) and body[at] not in _SPACE and (command or body[at] != "]"):
        at += 1
    return at


def _tag(body: str, at: int, command: bool) -> _Tag:
    """The tag whose name starts at ``at``; a ``@`` command runs to the line's end."""
    n = len(body)
    i = _bare_end(body, at, command)
    tag = _Tag(body[at:i].lower(), n)
    while True:
        while i < n and body[i] in _SPACE:
            i += 1
        if i >= n:
            return tag
        if body[i] == "]" and not command:
            tag.end = i + 1
            return tag
        key_at = i
        while i < n and body[i] not in _SPACE + "=" and (command or body[i] != "]"):
            i += 1
        key = body[key_at:i].lower()
        j = i
        while j < n and body[j] in _SPACE:
            j += 1
        if j >= n or body[j] != "=":
            continue
        j += 1
        while j < n and body[j] in _SPACE:
            j += 1
        if j < n and body[j] in "\"'":
            close = body.find(body[j], j + 1)
            end = n if close < 0 else close + 1
            value = body[j + 1 : n if close < 0 else close]
        else:
            end = _bare_end(body, j, command)
            value = body[j:end]
        tag.values.setdefault(key, (value, j, end))
        i = end


def _first_tag(body: str) -> str | None:
    stripped = body.lstrip(_SPACE)
    lead = len(body) - len(stripped)
    if stripped.startswith("@"):
        return _tag(body, lead + 1, True).name
    if stripped.startswith("[") and not stripped.startswith("[["):
        return _tag(body, lead + 1, False).name
    return None


class _Reader:
    def __init__(self, file: str, rules: KagRules) -> None:
        self.file = file
        self.rules = rules
        self.found: list[Piece] = []
        self.label = ""
        self.counts: dict[str, int] = {}
        self.speaker: str | None = None
        self.depth = 0  # > 0 inside a skipped branch
        self.script = False
        self.macro = False
        self.skipped_label = False

    def _names(self, tag: _Tag) -> list[tuple[str, int, int]]:
        return sorted(
            (
                tag.values[attribute]
                for name, attribute in self.rules.name_attributes
                if name == tag.name and attribute in tag.values
            ),
            key=lambda value: value[1],
        )

    def _breaks(self, tag: _Tag) -> bool:
        """Whether this tag ends the stretch of text before it."""
        if self.macro or self.depth:
            return False
        return (
            tag.name in ("iscript", "macro")
            or tag.name in self.rules.skip_branches
            or bool(self._names(tag))
        )

    def _tag(self, number: int, tag: _Tag) -> None:
        if self.macro:
            self.macro = tag.name != "endmacro"
        elif tag.name == "iscript":
            self.script = True
        elif tag.name == "macro":
            self.macro = True
        elif self.depth:
            if tag.name == "if" or tag.name in self.rules.skip_branches:
                self.depth += 1
            elif tag.name == "endif":
                self.depth -= 1
        elif tag.name in self.rules.skip_branches:
            self.depth = 1
        elif not self.skipped_label:
            for value, start, end in self._names(tag):
                if value and not value.startswith("&"):
                    self.found.append(
                        Piece(NAME_PREFIX + value, "name", value, None, number, start, end)
                    )
                    self.speaker = value
                else:
                    self.speaker = None

    def _flush(self, number: int, body: str, run: tuple[int, int] | None) -> None:
        if run is None:
            return
        n = self.counts.get(self.label, 0)
        self.counts[self.label] = n + 1
        self.found.append(
            Piece(
                f"{self.file}:{self.label}:{n}",
                "dialogue",
                body[run[0] : run[1]],
                self.speaker,
                number,
                *run,
            )
        )
        self.speaker = None

    def line(self, number: int, body: str) -> None:
        if self.script:
            self.script = _first_tag(body) != "endscript"
            return
        stripped = body.lstrip(_SPACE)
        lead = len(body) - len(stripped)
        if not stripped or stripped[0] == ";":
            return
        if stripped[0] == "*":
            self.label = stripped[1:].split("|", 1)[0].strip()
            self.skipped_label = self.rules.skips_label(self.label)
            return
        if stripped[0] == "@":
            self._tag(number, _tag(body, lead + 1, True))
            return
        run: tuple[int, int] | None = None
        i = lead
        while i < len(body):
            if body.startswith("[[", i):
                width = 2
            elif body[i] == "[":
                tag = _tag(body, i + 1, False)
                if self._breaks(tag):
                    self._flush(number, body, run)
                    run = None
                self._tag(number, tag)
                if self.script:
                    return
                i = tag.end
                continue
            else:
                width = 1
            if not (self.depth or self.macro or self.skipped_label) and body[i] not in _SPACE:
                run = (run[0] if run else i, i + width)
            i += width
        self._flush(number, body, run)


def _bodies(text: str) -> list[tuple[str, str]]:
    """Each physical line as (line without its ending, ending)."""
    out = []
    for raw in text.splitlines(keepends=True):
        body = raw.splitlines()[0]
        out.append((body, raw[len(body) :]))
    return out


def pieces(text: str, file: str, rules: KagRules) -> list[Piece]:
    """Every dialogue stretch and speaker name outside skipped regions, in file order."""
    reader = _Reader(file, rules)
    for number, (body, _ending) in enumerate(_bodies(text)):
        reader.line(number, body)
    return reader.found


def _closed_tags(text: str) -> set[str]:
    """Every whole ``[...]`` tag in the text, skipping ``[[`` escapes."""
    found = set()
    at = text.find("[")
    while at >= 0:
        if text.startswith("[[", at):
            at = text.find("[", at + 2)
            continue
        end = _tag(text, at + 1, False).end
        if text[end - 1] == "]":
            found.add(text[at:end])
        at = text.find("[", end)
    return found


def _dialogue(piece: Piece, target: str, body: str) -> str:
    allowed = _closed_tags(piece.source) | {"[r]"}
    at = target.find("[")
    while at >= 0:
        if target.startswith("[[", at):
            at = target.find("[", at + 2)
            continue
        written = target[at : _tag(target, at + 1, False).end]
        if written not in allowed:
            raise UnsafeTarget(
                f"{piece.id}: the translation has {written!r}, which is not a tag of the "
                "original line; write a literal [ as [[."
            )
        at = target.find("[", at + len(written))
    first = target.lstrip(_SPACE)[:1]
    if first in _LINE_START and not body[: piece.start].strip(_SPACE):
        raise UnsafeTarget(
            f"{piece.id}: the translation starts the line with {first!r}, which the game "
            f"reads as a {_LINE_START[first]}."
        )
    return _LINE_BREAK.sub("[r]", target)


def _name(piece: Piece, target: str) -> str:
    if '"' in target or _LINE_BREAK.search(target):
        raise UnsafeTarget(
            f"{piece.id}: a speaker name can hold neither a double quote nor a line break."
        )
    return f'"{target}"'


def rewrite(text: str, file: str, rules: KagRules, targets: Mapping[str, str]) -> str:
    """The script with each piece whose target is set (not blank, not its source) replaced."""
    lines = _bodies(text)
    edits: dict[int, list[tuple[int, int, str]]] = {}
    for piece in pieces(text, file, rules):
        target = targets.get(piece.id)
        if target is None or not target.strip() or target == piece.source:
            continue
        body = lines[piece.line][0]
        new = _name(piece, target) if piece.kind == "name" else _dialogue(piece, target, body)
        edits.setdefault(piece.line, []).append((piece.start, piece.end, new))
    for number, changes in edits.items():
        body, ending = lines[number]
        for start, end, new in sorted(changes, reverse=True):
            body = body[:start] + new + body[end:]
        lines[number] = (body, ending)
    return "".join(body + ending for body, ending in lines)


def decode_script(data: bytes) -> str:
    """A script's text: UTF-16LE or UTF-8 with a BOM, else UTF-8, else CP932."""
    if data.startswith(_SCRAMBLED):
        raise ScrambledText("The script is scrambled (it starts with FE FE).")
    try:
        if data.startswith(_BOM_UTF16):
            text = data[len(_BOM_UTF16) :].decode("utf-16-le")
        elif data.startswith(_BOM_UTF8):
            text = data[len(_BOM_UTF8) :].decode("utf-8")
        else:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("cp932")
    except UnicodeDecodeError as error:
        raise ScrambledText("The script is neither UTF-16, UTF-8 nor Shift-JIS text.") from error
    if _CONTROL.search(text):
        raise ScrambledText("The script holds control characters, so it is not plain text.")
    return text


def encode_script(text: str) -> bytes:
    """UTF-16LE with a BOM: KiriKiri reads it natively and it holds any language."""
    return _BOM_UTF16 + text.encode("utf-16-le")
