"""Sentences pylivemaker cuts wherever a variable is inserted into them."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

try:
    from livemaker.lsb.novel import BreakType, TWdChar, TWdOpeReturn, TWdType
except ImportError:  # pragma: no cover - exercised only without the extra
    pass

VAR_TOKEN = re.compile(r"<VAR ([^<>\n]+)>")
_TOKEN_NAME = re.compile(r"[^<>\n]+")


@dataclass(frozen=True)
class Run:
    """A sentence cut by variables, as a span of a TpWord body."""

    start: int
    end: int
    first_block: int
    last_block: int
    parts: tuple[dict, ...]


def _is_line_break(glyph) -> bool:
    return glyph.type == TWdType.TWdOpeReturn and glyph.break_type == BreakType.LINE


def _is_run_glyph(glyph) -> bool:
    return (
        glyph.type in (TWdType.TWdChar, TWdType.TWdOpeVar) or _is_line_break(glyph)
    )


def _spans(body: Sequence) -> list[tuple[int, int]]:
    """Maximal spans of run glyphs, without line breaks at either end."""
    spans = []
    index = 0
    while index < len(body):
        if not _is_run_glyph(body[index]):
            index += 1
            continue
        start = index
        while index < len(body) and _is_run_glyph(body[index]):
            index += 1
        end = index
        while start < end and _is_line_break(body[start]):
            start += 1
        while end > start and _is_line_break(body[end - 1]):
            end -= 1
        if start < end:
            spans.append((start, end))
    return spans


def find_runs(body: Sequence, blocks: Sequence) -> list[Run]:
    """The runs of ``body`` worth merging, in body order."""
    runs = []
    for start, end in _spans(body):
        glyphs = body[start:end]
        variables = [glyph for glyph in glyphs if glyph.type == TWdType.TWdOpeVar]
        if not variables or not any(glyph.type == TWdType.TWdChar for glyph in glyphs):
            continue
        if not all(_TOKEN_NAME.fullmatch(glyph.name) for glyph in variables):
            continue
        members = [
            (index, block)
            for index, block in enumerate(blocks)
            if start <= block.start < end
        ]
        parts: list[dict] = []
        cursor = start
        for index, block in members:
            parts.extend(_between(body, cursor, block.start))
            parts.append({"block": index})
            cursor = block.end
        parts.extend(_between(body, cursor, end))
        runs.append(
            Run(
                start=start,
                end=end,
                first_block=members[0][0],
                last_block=members[-1][0],
                parts=tuple(parts),
            )
        )
    return runs


def _between(body: Sequence, start: int, end: int) -> list[dict]:
    """What lies between blocks: variables, and line breaks as gap text."""
    parts: list[dict] = []
    for glyph in body[start:end]:
        if glyph.type == TWdType.TWdOpeVar:
            parts.append({"var": glyph.name})
        elif parts and "text" in parts[-1]:
            parts[-1]["text"] += "\n"
        else:
            parts.append({"text": "\n"})
    return parts


def run_source(body: Sequence, run: Run, decode: Callable[[str], str]) -> str:
    """The run as one source string, ``decode`` applied to its text only."""
    pieces: list[str] = []
    text: list[str] = []
    for glyph in body[run.start : run.end]:
        if glyph.type == TWdType.TWdOpeVar:
            pieces.append(decode("".join(text)))
            text.clear()
            pieces.append(f"<VAR {glyph.name}>")
        elif glyph.type == TWdType.TWdChar:
            text.append(str(glyph.ch))
        else:
            text.append("\n")
    pieces.append(decode("".join(text)))
    return "".join(pieces)


class VariableMismatch(ValueError):
    """A translation's variables are not the ones its sentence holds."""


_STYLE = ("decorator", "text_speed", "link_name", "link", "condition")


def render_run(body: Sequence, run: Run, target: str, encode: Callable[[str], str]) -> list:
    """The glyphs ``target`` puts in place of ``run``."""
    glyphs = body[run.start : run.end]
    originals: dict[str, list] = {}
    for glyph in glyphs:
        if glyph.type == TWdType.TWdOpeVar:
            originals.setdefault(glyph.name, []).append(glyph)
    wanted = Counter(VAR_TOKEN.findall(target))
    held = Counter({name: len(found) for name, found in originals.items()})
    if wanted != held:
        missing = sorted((held - wanted).elements())
        extra = sorted((wanted - held).elements())
        problems = []
        if missing:
            problems.append("missing " + ", ".join(f"<VAR {name}>" for name in missing))
        if extra:
            problems.append("unexpected " + ", ".join(f"<VAR {name}>" for name in extra))
        raise VariableMismatch("; ".join(problems))
    first_char = next(glyph for glyph in glyphs if glyph.type == TWdType.TWdChar)
    style = {attr: getattr(first_char, attr, None) for attr in _STYLE}
    rendered: list = []
    for index, piece in enumerate(VAR_TOKEN.split(target)):
        if index % 2:
            rendered.append(originals[piece].pop(0))
            continue
        for ch in encode(piece):
            if ch == "\n":
                rendered.append(TWdOpeReturn(break_type=BreakType.LINE, **style))
            else:
                rendered.append(TWdChar(ch=ch, **style))
    return rendered


def splice_runs(body: Sequence, edits: Sequence[tuple[Run, list]]) -> list:
    """``body`` with each run's span replaced by its glyphs."""
    spliced = list(body)
    for run, glyphs in sorted(edits, key=lambda edit: edit[0].start, reverse=True):
        spliced[run.start : run.end] = glyphs
    return spliced
