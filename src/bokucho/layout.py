"""Separating a unit's layout from the text inside it."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import accumulate

_ENDS_A_CLAUSE = frozenset("，、。．！？!?…‥；;：:」』）)】〉》〕］]～—,.")
_NEVER_STARTS_A_LINE = frozenset("，、。．！？!?…‥；;：:」』）)】〉》〕］]～,.")
_NEVER_ENDS_A_LINE = frozenset("「『（(【〈《〔［[")
_OFF_A_CLAUSE = 8


@dataclass(frozen=True, slots=True)
class LayoutLine:
    """One source line: either fixed whitespace, or text between two margins."""

    prefix: str = ""
    suffix: str = ""
    literal: str | None = None


@dataclass(frozen=True, slots=True)
class TextLayout:
    """Where a unit's text sat, so it can be put back exactly."""

    lines: tuple[LayoutLine, ...]

    @property
    def holes(self) -> int:
        return sum(1 for line in self.lines if line.literal is None)


def split_layout(
    source: str, include: Callable[[str], bool] | None = None,
) -> tuple[TextLayout, str]:
    """Separate ``source`` into its layout and the text to translate."""
    lines: list[LayoutLine] = []
    content: list[str] = []
    for line in source.split("\n"):
        stripped = line.strip()
        if not stripped or (include is not None and not include(stripped)):
            lines.append(LayoutLine(literal=line))
            continue
        start = len(line) - len(line.lstrip())
        end = len(line.rstrip())
        lines.append(LayoutLine(prefix=line[:start], suffix=line[end:]))
        content.append(stripped)
    return TextLayout(tuple(lines)), "\n".join(content)


def layout_content(layout: TextLayout, text: str) -> str | None:
    """Read translated positions; None if line counts or fixed content changed."""
    parts = text.split("\n")
    if len(parts) != len(layout.lines) or any(
        line.literal is not None and line.literal != part
        for line, part in zip(layout.lines, parts, strict=True)
    ):
        return None
    return "\n".join(
        part.strip()
        for line, part in zip(layout.lines, parts, strict=True)
        if line.literal is None
    )


def apply_layout(layout: TextLayout, translated: str) -> str | None:
    """Put ``translated`` back inside ``layout``."""
    parts = translated.split("\n")
    if len(parts) != layout.holes:
        return None
    remaining = iter(parts)
    return "\n".join(
        line.literal
        if line.literal is not None
        else f"{line.prefix}{next(remaining)}{line.suffix}"
        for line in layout.lines
    )


def reflow(
    layout: TextLayout,
    translated: str,
    region: str,
    keep: Sequence[re.Pattern[str]] = (),
) -> str | None:
    """Put ``translated`` back inside ``layout`` whatever its line count."""
    lines = [line.strip() for line in translated.split("\n") if line.strip()]
    if len(lines) == layout.holes:
        return apply_layout(layout, "\n".join(lines))
    if not lines:
        return None
    text = lines[0]
    for line in lines[1:]:
        text += ("" if _wide(text[-1]) or _wide(line[0]) else " ") + line
    columns = list(accumulate((_width(character) for character in text), initial=0))
    shares = list(accumulate(_width(line) or 1 for line in region.split("\n")))
    kept = [(match.start(), match.end()) for pattern in keep for match in pattern.finditer(text)]
    breaks = [0]
    for index in range(1, layout.holes):
        goal = columns[-1] * shares[index - 1] / shares[-1]
        best: tuple[float, int] | None = None
        for at in range(breaks[-1] + 1, len(text) - (layout.holes - index) + 1):
            cost = _break_cost(text, at, kept)
            if cost is None:
                continue
            score = abs(columns[at] - goal) + cost
            if best is None or score < best[0]:
                best = (score, at)
        if best is None:
            return None
        breaks.append(best[1])
    ends = [*breaks[1:], len(text)]
    pieces = [text[start:end].strip() for start, end in zip(breaks, ends, strict=True)]
    if not all(pieces):
        return None
    return apply_layout(layout, "\n".join(pieces))


def _break_cost(text: str, at: int, kept: list[tuple[int, int]]) -> int | None:
    """What breaking ``text`` before index ``at`` costs, None where it may not."""
    before, after = text[at - 1], text[at]
    if after in _NEVER_STARTS_A_LINE or before in _NEVER_ENDS_A_LINE:
        return None
    if any(start < at < end for start, end in kept):
        return None
    if before == " " or after == " ":
        return 0
    if not _wide(before) and not _wide(after):
        return None
    return 0 if before in _ENDS_A_CLAUSE else _OFF_A_CLAUSE


def _wide(character: str) -> bool:
    return unicodedata.east_asian_width(character) in ("W", "F")


def _width(text: str) -> int:
    return sum(2 if _wide(character) else 1 for character in text)
