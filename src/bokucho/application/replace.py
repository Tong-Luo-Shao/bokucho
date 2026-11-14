"""Replace one rendering with another across the project's translations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from dataclasses import dataclass

from bokucho.application.errors import ValidationFailure
from bokucho.application.glossary import delete_glossary, list_glossary_entries, upsert_glossary
from bokucho.application.projects import string_filename
from bokucho.application.text import keep_overwritten
from bokucho.model import Status
from bokucho.project import Project
from bokucho.qa import format_issues, markup_patterns
from bokucho.sync.json_io import held_string_file, load_string_file, string_paths

Mark = tuple[int, int]  # [start, end) of one occurrence, counted in code points


@dataclass(frozen=True, slots=True)
class ReplaceMatch:
    """One line a replace would change."""

    source_file: str
    unit_id: str
    source: str
    before: str
    after: str
    status: Status
    before_marks: tuple[Mark, ...]
    after_marks: tuple[Mark, ...]
    breaks_format: bool  # ``after`` fails the format check and ``before`` did not


@dataclass(frozen=True, slots=True)
class GlossaryChange:
    """One glossary entry whose translation holds the old rendering."""

    source: str
    before: str
    after: str
    origin: str  # "project" or "global"


@dataclass(frozen=True, slots=True)
class ReplacePreview:
    """Everything a replace would touch, read from disk and written nowhere."""

    matches: tuple[ReplaceMatch, ...]
    emptied: int  # lines left alone because the replace would leave them no translation
    pictures: tuple[str, ...]  # pictures whose text holds the old rendering; never changed
    glossary: tuple[GlossaryChange, ...]
    fingerprint: str  # moves when the lines that match are no longer the ones listed


def replace_in_target(
    target: str, find: str, replacement: str, engine: str
) -> tuple[str, tuple[Mark, ...], tuple[Mark, ...]]:
    """``target`` with ``find`` replaced between markup, and where it stood and landed.

    Tags, variables and control codes of ``engine`` are never searched, so a term that
    markup cuts in two is not found.
    """
    if not find:
        return target, (), ()
    tokens = re.compile("|".join(f"(?:{regex.pattern})" for regex in markup_patterns(engine)))
    pieces: list[str] = []
    before_marks: list[Mark] = []
    after_marks: list[Mark] = []
    landed = 0  # length of the text built so far

    def keep(piece: str) -> None:
        nonlocal landed
        pieces.append(piece)
        landed += len(piece)

    def take(start: int, end: int) -> None:
        cursor = start
        while (hit := target.find(find, cursor, end)) != -1:
            keep(target[cursor:hit])
            before_marks.append((hit, hit + len(find)))
            after_marks.append((landed, landed + len(replacement)))
            keep(replacement)
            cursor = hit + len(find)
        keep(target[cursor:end])

    cursor = 0
    for token in tokens.finditer(target):
        take(cursor, token.start())
        keep(token.group())
        cursor = token.end()
    take(cursor, len(target))
    return "".join(pieces), tuple(before_marks), tuple(after_marks)


def preview_replace(project: Project, find: str, replacement: str) -> ReplacePreview:
    """What replacing ``find`` with ``replacement`` would change in the project."""
    if not find:
        raise ValidationFailure("Type the text to find.")
    if find == replacement:
        raise ValidationFailure("Find and Replace with are the same.")
    engine = project.config.engine.type
    target_lang = project.config.target_lang

    def fails_format(source: str, target: str, script: bool) -> bool:
        return bool(
            format_issues(source, target, engine=engine, target_lang=target_lang, script=script)
        )

    matches: list[ReplaceMatch] = []
    pictures: list[str] = []
    emptied = 0
    for path in string_paths(project.strings_dir):
        string_file = load_string_file(path)
        for unit in string_file.units:
            after, before_marks, after_marks = replace_in_target(
                unit.target, find, replacement, engine
            )
            if not before_marks:
                continue
            if unit.kind == "image":
                # The text is one half of a pair with a drawn picture; changing it alone
                # would leave the picture showing the old rendering.
                pictures.append(str(unit.engine_ref.get("image", unit.id)))
                continue
            if not after.strip():
                # The line would keep a status that says it has a translation.
                emptied += 1
                continue
            script = unit.kind == "script"
            matches.append(
                ReplaceMatch(
                    source_file=string_file.source_file,
                    unit_id=unit.id,
                    source=unit.source,
                    before=unit.target,
                    after=after,
                    status=unit.status,
                    before_marks=before_marks,
                    after_marks=after_marks,
                    breaks_format=fails_format(unit.source, after, script)
                    and not fails_format(unit.source, unit.target, script),
                )
            )
    glossary = tuple(
        GlossaryChange(entry.source, entry.target, renamed, origin)
        for entry, origin in list_glossary_entries(project)
        if origin != "shadowed"
        and find in entry.target
        and (renamed := entry.target.replace(find, replacement))
    )
    shown = [[match.source_file, match.unit_id, match.before] for match in matches]
    return ReplacePreview(
        matches=tuple(matches),
        emptied=emptied,
        pictures=tuple(pictures),
        glossary=glossary,
        fingerprint=hashlib.sha256(json.dumps(shown).encode("ascii")).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class ReplaceUndo:
    """What one replace wrote, enough to take it back."""

    find: str
    replacement: str
    lines: tuple[tuple[str, str, str, str], ...]  # (source_file, unit_id, before, after)
    #: (source, the project entry's translation before); ``None`` when the replace made the entry.
    glossary: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    lines: int
    occurrences: int
    glossary: int
    breaks_format: int
    undo: ReplaceUndo


@dataclass(frozen=True, slots=True)
class UndoResult:
    restored: int
    skipped: int  # lines edited since the replace, left as they are
    glossary: int


def apply_replace(
    project: Project,
    find: str,
    replacement: str,
    fingerprint: str,
    excluded: Collection[tuple[str, str]] = (),
    glossary: Collection[str] = (),
) -> ReplaceResult:
    """Write the replace that was previewed under ``fingerprint``.

    ``excluded`` names the ``(source_file, unit_id)`` lines left out in the preview and
    ``glossary`` the sources of the glossary entries to change. A line keeps its status,
    and its old translation goes into its history.
    """
    # No cancellation token: a replace stopped half way would leave lines changed with no
    # undo record.
    preview = preview_replace(project, find, replacement)
    if preview.fingerprint != fingerprint:
        raise ValidationFailure(
            "The translations changed since the preview. Look at the preview again."
        )
    left_out = {tuple(line) for line in excluded}
    wanted: dict[str, dict[str, ReplaceMatch]] = {}
    for match in preview.matches:
        if (match.source_file, match.unit_id) not in left_out:
            wanted.setdefault(match.source_file, {})[match.unit_id] = match
    lines: list[tuple[str, str, str, str]] = []
    occurrences = breaks_format = 0
    for source_file, matches in wanted.items():
        filename = string_filename(source_file)
        with held_string_file(project.strings_dir / filename) as string_file:
            for unit in string_file.units:
                match = matches.get(unit.id)
                # A line that changed in the instant since the preview was not shown as it is.
                if match is None or unit.target != match.before:
                    continue
                keep_overwritten(unit)
                unit.target = match.after
                lines.append((source_file, unit.id, match.before, match.after))
                occurrences += len(match.before_marks)
                breaks_format += match.breaks_format
            project.save_string_file(string_file, filename)
    chosen = set(glossary)
    notes = {
        entry.source: entry.note
        for entry, origin in list_glossary_entries(project)
        if origin != "shadowed"
    }
    changed: list[tuple[str, str | None]] = []
    for change in preview.glossary:
        if change.source not in chosen:
            continue
        # Always the project's own glossary: a shared entry gains a project entry that takes
        # precedence, and the glossary every project reads is left as it is.
        upsert_glossary(
            project, change.source, change.after, note=notes.get(change.source), scope="project"
        )
        changed.append((change.source, change.before if change.origin == "project" else None))
    return ReplaceResult(
        lines=len(lines),
        occurrences=occurrences,
        glossary=len(changed),
        breaks_format=breaks_format,
        undo=ReplaceUndo(find, replacement, tuple(lines), tuple(changed)),
    )


def undo_replace(project: Project, undo: ReplaceUndo) -> UndoResult:
    """Take ``undo``'s replace back wherever a line still holds what it wrote."""
    # No cancellation token, for the reason apply_replace has none.
    written: dict[str, dict[str, tuple[str, str]]] = {}
    for source_file, unit_id, before, after in undo.lines:
        written.setdefault(source_file, {})[unit_id] = (before, after)
    restored = 0
    for source_file, lines in written.items():
        filename = string_filename(source_file)
        if not (project.strings_dir / filename).is_file():
            continue
        with held_string_file(project.strings_dir / filename) as string_file:
            for unit in string_file.units:
                before, after = lines.get(unit.id, (None, None))
                if after is None or unit.target != after:
                    continue
                keep_overwritten(unit)
                unit.target = before
                restored += 1
            project.save_string_file(string_file, filename)
    for source, old_target in undo.glossary:
        if old_target is None:
            delete_glossary(project, source, scope="project")
        else:
            upsert_glossary(project, source, old_target, scope="project")
    return UndoResult(
        restored=restored, skipped=len(undo.lines) - restored, glossary=len(undo.glossary)
    )
