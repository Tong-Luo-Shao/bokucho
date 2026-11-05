"""Measuring how a translation run behaved, over what is already on disk."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from bokucho.application.text import effective_glossary
from bokucho.layout import split_layout
from bokucho.model import TextUnit
from bokucho.project import Project
from bokucho.qa import ENGINE_CODECS, format_issues

_KANA_RE = re.compile(r"[぀-ゟ゠-ヺー-ヿ]")


@dataclass(frozen=True, slots=True)
class Property:
    key: str
    description: str
    failures: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass(frozen=True, slots=True)
class VerifyReport:
    examined: int
    properties: tuple[Property, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(prop.ok for prop in self.properties)


def _layout_failures(units: Iterable[tuple[str, TextUnit]]) -> list[tuple[str, str]]:
    failures = []
    for unit_id, unit in units:
        layout, _ = split_layout(unit.source)
        target_layout, _ = split_layout(unit.target)
        if len(layout.lines) != len(target_layout.lines):
            failures.append(
                (unit_id, f"{len(layout.lines)} line(s) became {len(target_layout.lines)}")
            )
            continue
        margins = [
            (source.prefix, source.suffix, source.literal)
            for source in layout.lines
        ]
        rebuilt = [
            (target.prefix, target.suffix, target.literal)
            for target in target_layout.lines
        ]
        if margins != rebuilt:
            failures.append((unit_id, "indentation or padding differs from the source"))
    return failures


def _tag_failures(
    units: Iterable[tuple[str, TextUnit]],
    engine: str,
) -> list[tuple[str, str]]:
    return [
        (unit_id, "; ".join(issues))
        for unit_id, unit in units
        if (
            issues := format_issues(
                unit.source, unit.target, engine=engine, script=unit.kind == "script"
            )
        )
    ]


def _kana_failures(units: Iterable[tuple[str, TextUnit]]) -> list[tuple[str, str]]:
    failures = []
    for unit_id, unit in units:
        residue = sorted(set(_KANA_RE.findall(unit.target)))
        if residue:
            shown = "".join(residue[:10]) + ("…" if len(residue) > 10 else "")
            failures.append((unit_id, f"{len(residue)} kana character(s): {shown}"))
    return failures


def _encoding_failures(
    units: Iterable[tuple[str, TextUnit]],
    codec: str | None,
    proxy_chars: frozenset[str],
) -> list[tuple[str, str]]:
    if not codec:
        return []
    failures = []
    for unit_id, unit in units:
        if unit.kind == "image":
            continue
        bad = sorted({ch for ch in unit.target if ch not in proxy_chars and not _fits(ch, codec)})
        if bad:
            shown = "".join(bad[:10]) + ("…" if len(bad) > 10 else "")
            failures.append((unit_id, f"{len(bad)} char(s) outside {codec}: {shown}"))
    return failures


def _glossary_failures(
    units: Iterable[tuple[str, TextUnit]],
    glossary: dict[str, str],
) -> list[tuple[str, str]]:
    failures = []
    for unit_id, unit in units:
        ignored = [
            source
            for source, target in glossary.items()
            if target and source in unit.source and target not in unit.target
        ]
        if ignored:
            failures.append((unit_id, f"terms not used: {', '.join(ignored[:5])}"))
    return failures


def _fits(ch: str, codec: str) -> bool:
    try:
        ch.encode(codec)
        return True
    except UnicodeEncodeError:
        return False


def verify_project(project: Project, proxy_chars: frozenset[str] = frozenset()) -> VerifyReport:
    """Measure every translated unit against the properties a run should hold."""
    units = [
        (f"{string_file.source_file} {unit.id}", unit)
        for string_file in project.load_string_files()
        for unit in string_file.units
        if unit.target.strip()
    ]
    codec = ENGINE_CODECS.get(project.config.engine.type)
    glossary = effective_glossary(project)
    return VerifyReport(
        examined=len(units),
        properties=(
            Property(
                "layout",
                "indentation, padding and spacer lines match the source",
                tuple(_layout_failures(units)),
            ),
            Property(
                "tags",
                "variables, tags and control codes survived translation",
                tuple(_tag_failures(units, project.config.engine.type)),
            ),
            Property(
                "script",
                "no source-script characters remain in the target",
                tuple(_kana_failures(units)),
            ),
            Property(
                "encoding",
                "every character survives the engine codec or the proxy map",
                tuple(_encoding_failures(units, codec, proxy_chars)),
            ),
            Property(
                "glossary",
                "agreed terms were used where their source appears",
                tuple(_glossary_failures(units, glossary)),
            ),
        ),
    )
