"""The Scripts file of XP, VX and VX Ace: names and Ruby sources, changed sources spliced back."""

from __future__ import annotations

import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass

from bokucho.engines.rpgmaker_marshal import LegacyData, LegacyString, load_legacy, splice
from bokucho.engines.ruby_literals import (
    Literal,
    Rule,
    UnreadableScript,
    compared,
    encode,
    literals,
    placeholder_mismatch,
    placeholders,
    rule,
)
from bokucho.model import Status

#: ``#{``, ``#@`` or ``#$`` after an even run of backslashes: an interpolation Ruby runs.
# ponytail: nested ones inside a live ``#{...}`` count too, so a literal that mixes an
# escaped ``#\{`` with a live ``#{...}`` holding a nested string can pass; interpolations
# reported by the scanner itself are the upgrade.
_LIVE = re.compile(r"(?<!\\)(?:\\\\)*#[{@$]")


@dataclass
class ScriptEntry:
    """One script: its place in the array, its name, and its source unless that cannot be read."""

    index: int
    name: str
    source: str | None
    problem: str
    code: LegacyString


@dataclass
class ScriptsFile:
    data: LegacyData
    entries: list[ScriptEntry]


@dataclass(frozen=True)
class ScriptText:
    """One literal a script shows or uses, with the status rules 1-5 give it."""

    address: str
    text: str
    script: str
    line: int
    status: Status
    key: bool
    asset: str | None


class ScriptRefused(RuntimeError):
    """A translated script text cannot be written back without changing what the script does."""


def _inflate(code: bytes) -> tuple[str | None, str]:
    """The source in ``code`` and why it is missing when it is."""
    try:
        raw = zlib.decompress(code)
    except zlib.error as exc:
        return None, f"the code does not inflate: {exc}"
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "the code is not UTF-8 text"


def load_scripts(blob: bytes) -> ScriptsFile:
    """Every ``[Integer, String, String]`` of a Scripts file; the code of each inflated."""
    data = load_legacy(blob)
    if not isinstance(data.root, list):
        raise ValueError("the Scripts file is not an array")
    entries = []
    for index, item in enumerate(data.root):
        if not (
            isinstance(item, list)
            and len(item) == 3
            and type(item[0]) is int
            and isinstance(item[1], LegacyString)
            and isinstance(item[2], LegacyString)
        ):
            raise ValueError(f"element {index} of Scripts is not [Integer, String, String]")
        _, name, code = item
        source, problem = _inflate(code)
        name = name.decode("utf-8", errors="replace")
        entries.append(ScriptEntry(index, name, source, problem, code))
    return ScriptsFile(data, entries)


def dump_scripts(file: ScriptsFile, sources: Mapping[int, str]) -> bytes:
    """The file's own bytes with only the changed sources' code replaced, checked by a reload."""
    by_index = {entry.index: entry for entry in file.entries}
    changed = {}
    for index, source in sources.items():
        if index not in by_index:
            raise ValueError(f"the Scripts file has no element {index}")
        if source != by_index[index].source:
            changed[index] = source
    if not changed:
        return file.data.blob
    spans = {}
    for index, source in changed.items():
        code = by_index[index].code
        spans[(code.start, code.end)] = zlib.compress(source.encode("utf-8"))
    out = splice(file.data.blob, spans)
    wanted = [changed.get(entry.index, entry.source) for entry in file.entries]
    if wanted != [entry.source for entry in load_scripts(out).entries]:
        raise ValueError("the translated scripts did not survive splicing")
    return out


_FONT_FAMILY = re.compile(r"Bokucho [A-Za-z0-9 ]+")
_MAGIC_COMMENT = re.compile(r"#.*coding[:=]")
#: Ruby 1.8 and 1.9 alike; if anything fails the game keeps its own fonts. F12 runs every
#: script again in the same Ruby, so the setter is wrapped only the first time. The font is
#: not loaded here: one loaded once an RGSS2 game was running went unused, while RGSS2
#: and RGSS3 load the Fonts folder at startup, so the fill font is put there.
_FONT_LOADER = """\
# Bokucho: ask first for the font that draws the translation.
begin
  class << Font
    unless method_defined?(:bokucho_default_name=)
      alias_method :bokucho_default_name=, :default_name=
      def default_name=(names)
        names = names.is_a?(Array) ? names : [names]
        self.bokucho_default_name = ['{family}'] + (names - ['{family}'])
      end
    end
  end
  Font.default_name = Font.default_name
rescue Exception
end
"""


def with_font_loader(blob: bytes, family: str) -> bytes:
    """The Scripts file asking for ``family`` first, before the first script's own code.

    Prepended rather than added as a script of its own: a new array element would shift
    every object and symbol link after it.
    """
    if not _FONT_FAMILY.fullmatch(family):
        raise ValueError(f"not a plain font name: {family!r}")
    scripts = load_scripts(blob)
    if not scripts.entries or scripts.entries[0].source is None:
        raise ValueError("the first script cannot be read")
    source = scripts.entries[0].source
    head, _, rest = source.partition("\n")
    keep = f"{head}\n" if _MAGIC_COMMENT.match(head) else ""
    loader = _FONT_LOADER.format(family=family)
    return dump_scripts(scripts, {0: keep + loader + (rest if keep else source)})


def _address(entry: ScriptEntry, literal: Literal) -> str:
    return f"s{entry.index}:{literal.line}:{literal.column}"


def _parsed(file: ScriptsFile) -> tuple[list[tuple[ScriptEntry, str, list[Literal]]], list[str]]:
    """The literals of each readable script, and why each other script is not read."""
    found, problems = [], []
    for entry in file.entries:
        if entry.source is None:
            problems.append(f"{entry.name}: {entry.problem}")
            continue
        try:
            found.append((entry, entry.source, literals(entry.source)))
        except UnreadableScript as exc:
            problems.append(f"{entry.name}: {exc}")
    return found, problems


def script_texts(
    file: ScriptsFile, assets: Mapping[str, str]
) -> tuple[list[ScriptText], list[str]]:
    """The literals rules 1-5 keep, with their status, and one warning per script left out."""
    found, problems = _parsed(file)
    keys = frozenset(
        literal.text
        for _, source, listed in found
        for literal in listed
        if compared(source, literal)
    )
    names = frozenset(assets)
    texts = []
    for entry, source, listed in found:
        for literal in listed:
            kind = rule(source, literal, keys, names)
            if kind is Rule.SKIP:
                continue
            shown = kind is Rule.DISPLAY or (kind is Rule.OTHER and not literal.text.isascii())
            texts.append(
                ScriptText(
                    address=_address(entry, literal),
                    text=literal.text,
                    script=entry.name,
                    line=literal.line,
                    status=Status.UNTRANSLATED if shown else Status.IGNORED,
                    key=kind is Rule.KEY,
                    asset=assets.get(literal.text) if kind is Rule.CODE else None,
                )
            )
    return texts, problems


def rewrite_scripts(
    file: ScriptsFile, targets: Mapping[str, tuple[str, str]]
) -> tuple[bytes, list[str]]:
    """The file with each target in its literal's place, or its own bytes and every refusal.

    ``targets`` maps an address to the text extracted there and its translation.
    """
    found, _ = _parsed(file)
    at = {
        _address(entry, literal): (entry, literal)
        for entry, _, listed in found
        for literal in listed
    }
    names = {f"s{entry.index}": entry.name for entry in file.entries}
    refusals: list[str] = []
    edits: dict[int, dict[int, str]] = {}
    for address, (extracted, target) in targets.items():
        if address not in at:
            script, _, rest = address.partition(":")
            refusals.append(
                f"{names.get(script, 'Scripts')} line {rest.partition(':')[0]} ({address}): "
                "no literal of the game's scripts starts there"
            )
            continue
        entry, literal = at[address]
        where = f"{entry.name} line {literal.line} ({address})"
        refusal = _edit_refusal(where, literal, extracted, target)
        if refusal is not None:
            refusals.append(refusal)
        elif target != literal.text:
            edits.setdefault(entry.index, {})[literal.start] = target

    sources: dict[int, str] = {}
    for entry, source, listed in found:
        changes = edits.get(entry.index)
        if not changes:
            continue
        rewritten = _rewritten(source, listed, changes)
        if rewritten is None:
            first = next(literal for literal in listed if literal.start in changes)
            refusals.append(
                f"{entry.name} line {first.line} ({_address(entry, first)}): "
                "the rewritten script no longer reads back as the same literals"
            )
            continue
        sources[entry.index] = rewritten
    if refusals:
        return file.data.blob, refusals
    return dump_scripts(file, sources), []


def _edit_refusal(where: str, literal, extracted: str, target: str) -> str | None:
    """Why ``target`` cannot replace ``literal``, or None when it can."""
    if literal.text != extracted:
        return f"{where}: the script changed since extraction; extract again"
    if target == literal.text:
        return None
    shown = sum(n for mark, n in placeholders(literal.text).items() if mark[:2] == "#{")
    if literal.quote == '"' and shown > len(_LIVE.findall(literal.raw)):
        return f"{where}: the literal shows a #{{...}} that Ruby does not run"
    missing, unexpected = placeholder_mismatch(literal.text, target)
    if missing or unexpected:
        said = [
            f"{verb} {', '.join(marks)}"
            for verb, marks in (("drops", missing), ("adds", unexpected))
            if marks
        ]
        return f"{where}: the translation {' and '.join(said)}"
    return None


def _rewritten(source: str, listed: list, changes: dict[int, str]) -> str | None:
    """``source`` with ``changes`` in place, or None if it no longer reads back as intended."""
    pieces, kept, shift = [], 0, 0
    expected = []
    for literal in listed:
        target = changes.get(literal.start)
        expected.append((literal.start + shift, literal.text if target is None else target))
        if target is not None:
            raw = encode(target, literal.quote)
            pieces += [source[kept : literal.start + 1], raw]
            kept = literal.end - 1
            shift += len(raw) - len(literal.raw)
    pieces.append(source[kept:])
    rewritten = "".join(pieces)
    try:
        read_back = [(literal.start, literal.text) for literal in literals(rewritten)]
    except UnreadableScript:
        read_back = []
    return rewritten if read_back == expected else None
