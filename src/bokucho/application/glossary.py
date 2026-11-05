"""Glossary mutation and occurrence-count use cases."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from bokucho.application.errors import ValidationFailure
from bokucho.model import StringFile
from bokucho.project import Project
from bokucho.translate.glossary import (
    Glossary,
    GlossaryEntry,
    global_glossary_path,
    load_global_glossary,
)

_log = logging.getLogger(__name__)


def _load_glossary(project: Project) -> Glossary:
    return Glossary.load_or_empty(project.glossary_path)


def _glossary_path(project: Project, scope: str) -> Path:
    """The on-disk path a given scope writes to (project or global)."""
    if scope == "global":
        path = global_glossary_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    if scope != "project":
        raise ValidationFailure(f"Unknown glossary scope: {scope!r}")
    return project.glossary_path


def upsert_glossary(
    project: Project,
    source: str,
    target: str,
    note: str | None = None,
    scope: str = "project",
) -> bool:
    """Insert or update one glossary entry; return whether it was created."""
    if not source or not target:
        raise ValidationFailure("Glossary source and target are required.")
    path = _glossary_path(project, scope)
    glossary = Glossary.load_or_empty(path)
    matches = [entry for entry in glossary.entries if entry.source == source]
    created = not matches
    if created:
        glossary.entries.append(GlossaryEntry(source=source, target=target, note=note))
    else:
        entry = matches[0]
        entry.target = target
        entry.note = note if note is not None else entry.note
        glossary.entries = [
            candidate
            for candidate in glossary.entries
            if candidate.source != source or candidate is entry
        ]
    glossary.save(path)
    return created


def delete_glossary(project: Project, source: str, scope: str = "project") -> bool:
    """Delete all entries for ``source`` in one scope; return whether anything changed."""
    path = _glossary_path(project, scope)
    glossary = Glossary.load_or_empty(path)
    retained = [entry for entry in glossary.entries if entry.source != source]
    if len(retained) == len(glossary.entries):
        return False
    glossary.entries = retained
    glossary.save(path)
    return True


def _safe_global_glossary() -> Glossary:
    """The global glossary, or empty when the file is missing or corrupt."""
    try:
        return load_global_glossary()
    except Exception:  # noqa: BLE001 - a corrupt user-level file just disables it
        _log.warning("Ignoring corrupt global glossary.")
        return Glossary()


def list_glossary_entries(project: Project) -> list[tuple[GlossaryEntry, str]]:
    """Project + global glossary entries tagged with their origin."""
    project_entries = _load_glossary(project).entries
    project_sources = {entry.source for entry in project_entries}
    tagged: list[tuple[GlossaryEntry, str]] = [
        (entry, "project") for entry in project_entries
    ]
    for entry in _safe_global_glossary().entries:
        origin = "shadowed" if entry.source in project_sources else "global"
        tagged.append((entry, origin))
    return tagged


def glossary_spans(mapping: dict[str, str], source: str) -> list[tuple[int, int]]:
    """Non-overlapping ``(start, end)`` spans of glossary terms in ``source``."""
    taken: list[tuple[int, int]] = []
    for term in sorted((t for t in mapping if t), key=len, reverse=True):
        start = source.find(term)
        while start != -1:
            end = start + len(term)
            if all(end <= s or start >= e for s, e in taken):
                taken.append((start, end))
            start = source.find(term, start + 1)
    return sorted(taken)


def glossary_occurrences(
    project: Project, string_files: Sequence[StringFile] | None = None
) -> dict[str, int]:
    """Count source-term substrings across project source and target strings."""
    entries = _load_glossary(project).entries
    if string_files is None:
        string_files = project.load_string_files()
    units = [unit for string_file in string_files for unit in string_file.units]
    sources = "\0".join(unit.source for unit in units)
    targets = "\0".join(unit.target for unit in units)
    return {
        entry.source: sources.count(entry.source) + targets.count(entry.source)
        if entry.source
        else 0
        for entry in entries
    }
