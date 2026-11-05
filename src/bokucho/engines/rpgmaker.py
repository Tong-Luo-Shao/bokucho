"""RPG Maker engine adapter — MV, MZ, XP, VX and VX Ace."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressLevel,
    ProgressSink,
)
from bokucho.engines.base import (
    EngineAdapter,
    archive_digest,
    holds_original,
    unpacked_stamp,
)
from bokucho.model import Context, Status, TextUnit

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()


class NotRpgMaker(RuntimeError):
    """The path does not look like an RPG Maker game."""


class RpgMakerWriteError(RuntimeError):
    """A data file could not be written back faithfully."""


class LineCountMismatch(RuntimeError):
    """A translation did not come back as the number of lines the game has room for."""


class Variant(StrEnum):
    MV = "mv"
    MZ = "mz"
    XP = "xp"
    VX = "vx"
    ACE = "ace"


RGSS_VARIANTS = frozenset({Variant.XP, Variant.VX, Variant.ACE})

VARIANT_ENGINES = {
    Variant.MV: "rpgmaker-mv",
    Variant.MZ: "rpgmaker-mz",
    Variant.XP: "rpgmaker-xp",
    Variant.VX: "rpgmaker-vx",
    Variant.ACE: "rpgmaker-vx-ace",
}

DATA_SUFFIX = {
    Variant.MV: ".json",
    Variant.MZ: ".json",
    Variant.XP: ".rxdata",
    Variant.VX: ".rvdata",
    Variant.ACE: ".rvdata2",
}

_ARCHIVE_GLOB = {Variant.ACE: "*.rgss3a", Variant.XP: "*.rgssad", Variant.VX: "*.rgss2a"}


@dataclass(frozen=True)
class Layout:
    """Where one game keeps its data, and which generation wrote it."""

    variant: Variant
    root: Path
    data_dir: Path
    archive: Path | None = None

    @property
    def relative_data(self) -> Path:
        """The data directory as the game itself names it (``www/data`` or ``data``)."""
        return self.data_dir.relative_to(self.root)


def sniff(game_path: Path) -> Layout:
    """The layout of the game at ``game_path``, which may be a file the user picked."""
    target = Path(game_path)
    root = target if target.is_dir() else target.parent
    for base in (root / "www", root):
        data_dir = base / "data"
        if (data_dir / "System.json").is_file():
            variant = (
                Variant.MZ if (base / "js" / "rmmz_core.js").is_file() else Variant.MV
            )
            return Layout(variant, root, data_dir)
    data_dir = root / "Data"
    for variant, pattern in _ARCHIVE_GLOB.items():
        if (data_dir / f"System{DATA_SUFFIX[variant]}").is_file():
            return Layout(variant, root, data_dir, None)
        archive = next(iter(sorted(root.glob(pattern))), None)
        if archive is not None:
            return Layout(variant, root, data_dir, archive)
    raise NotRpgMaker(
        f"No RPG Maker data found under {root} — expected data/System.json "
        f"(MV, MZ), Data/System.rxdata or a .rgssad archive (XP), "
        f"Data/System.rvdata or a .rgss2a archive (VX), or "
        f"Data/System.rvdata2 or a .rgss3a archive (VX Ace)."
    )


_COMPACT: dict[str, Any] = {"ensure_ascii": False, "separators": (",", ":")}


_UNREADABLE_ERRORS = (ValueError, AttributeError)

_LEGACY_SUFFIXES = frozenset({DATA_SUFFIX[Variant.XP], DATA_SUFFIX[Variant.VX]})


def load_data(path: Path) -> Any:
    """``path``'s content, by whichever format its own name says it is."""
    suffix = path.suffix.lower()
    if suffix == ".rvdata2":
        from bokucho.engines.rpgmaker_marshal import load_marshal

        return load_marshal(path.read_bytes())
    if suffix in _LEGACY_SUFFIXES:
        from bokucho.engines.rpgmaker_marshal import load_legacy

        return load_legacy(path.read_bytes())
    return json.loads(path.read_text(encoding="utf-8"))


def dumps_data(payload: Any) -> str:
    """``payload`` in the editor's own shape."""
    if isinstance(payload, list):
        inner = ",\n".join(json.dumps(item, **_COMPACT) for item in payload)
        return f"[\n{inner}\n]"
    return json.dumps(payload, **_COMPACT)


def write_data(path: Path, payload: Any) -> None:
    """Write ``payload`` to ``path``, refusing anything that did not survive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".rvdata2":
        from bokucho.engines.rpgmaker_marshal import dump_marshal, load_marshal

        blob = dump_marshal(payload)
        if repr(load_marshal(blob)) != repr(payload):
            raise RpgMakerWriteError(f"{path.name} did not survive serialization")
        path.write_bytes(blob)
        return
    if path.suffix.lower() in _LEGACY_SUFFIXES:
        from bokucho.engines.rpgmaker_marshal import dump_legacy

        try:
            blob = dump_legacy(payload)
        except ValueError as exc:
            raise RpgMakerWriteError(f"{path.name} did not survive serialization: {exc}") from exc
        path.write_bytes(blob)
        return
    text = dumps_data(payload)
    if json.loads(text) != payload:
        raise RpgMakerWriteError(f"{path.name} did not survive serialization")
    path.write_text(text, encoding="utf-8")


def _walkable(payload: Any, variant: Variant) -> Any:
    """``payload`` as the walk in ``file_slots`` expects to read it."""
    if variant in RGSS_VARIANTS:
        from bokucho.engines.rpgmaker_marshal import view

        return view(payload if variant is Variant.ACE else payload.root)
    return payload


_RUN_CODES = frozenset({401, 405})
_HEADER = 101
_CHOICES = 102


@dataclass
class Slot:
    """One addressable string inside a parsed data tree."""

    address: str
    kind: str
    text: str
    speaker: str = ""
    commands: list[dict] | None = None
    container: list | dict | None = None
    key: str | int | None = None
    tags: tuple[str, ...] = ()

    def write(self, target: str) -> None:
        """Put ``target`` back where this Slot's text came from."""
        if self.commands is None:
            self.container[self.key] = target  # type: ignore[index]
            return
        lines = target.split("\n")
        if len(lines) != len(self.commands):
            raise LineCountMismatch(
                f"{self.address}: the game holds {len(self.commands)} line(s), "
                f"the translation has {len(lines)}"
            )
        for command, line in zip(self.commands, lines, strict=True):
            command["parameters"][0] = line


def command_slots(
    commands: list[dict], address: str, variant: Variant
) -> list[Slot]:
    """Every translatable string in one event command list."""
    slots: list[Slot] = []
    run: list[dict] = []
    run_code = 0
    start = 0
    speaker = ""

    def flush() -> None:
        nonlocal run, run_code
        if not run:
            return
        slots.append(
            Slot(
                address=f"{address}:c{start}",
                kind="dialogue",
                text="\n".join(command["parameters"][0] for command in run),
                speaker=speaker if run_code == 401 else "",
                commands=list(run),
            )
        )
        run, run_code = [], 0

    for index, command in enumerate(commands):
        code = command.get("code")
        opens = variant is Variant.XP and code == _HEADER
        parameters = command.get("parameters")
        #: A string that is not UTF-8 is never text, so it ends the run it would have joined.
        if (opens or code in _RUN_CODES) and parameters and isinstance(parameters[0], str):
            if opens or code != run_code:
                flush()
                start, run_code = index, 401 if opens else code
            run.append(command)
            continue
        flush()
        if code == _HEADER:
            parameters = command.get("parameters") or []
            speaker = parameters[4] if variant is Variant.MZ and len(parameters) > 4 else ""
        elif code == _CHOICES:
            slots += _choice_slots(command, f"{address}:c{index}")
    flush()
    return slots


def _choice_slots(command: dict, address: str) -> list[Slot]:
    choices = (command.get("parameters") or [[]])[0]
    return [
        Slot(
            address=f"{address}:{position}",
            kind="choice",
            text=choice,
            container=choices,
            key=position,
        )
        for position, choice in enumerate(choices)
        if isinstance(choice, str)
    ]


DATABASE_FIELDS: dict[str, tuple[str, ...]] = {
    "Actors": ("name", "nickname", "profile"),
    "Classes": ("name",),
    "Skills": ("name", "description", "message1", "message2"),
    "Items": ("name", "description"),
    "Weapons": ("name", "description"),
    "Armors": ("name", "description"),
    "Enemies": ("name",),
    "States": ("name", "message1", "message2", "message3", "message4"),
}

ACE_DATABASE_FIELDS: dict[str, tuple[str, ...]] = {
    **DATABASE_FIELDS,
    "Actors": ("name", "nickname", "description"),
}

VX_DATABASE_FIELDS: dict[str, tuple[str, ...]] = {**DATABASE_FIELDS, "Actors": ("name",)}

XP_DATABASE_FIELDS: dict[str, tuple[str, ...]] = {
    "Actors": ("name",),
    "Classes": ("name",),
    "Skills": ("name", "description"),
    "Items": ("name", "description"),
    "Weapons": ("name", "description"),
    "Armors": ("name", "description"),
    "Enemies": ("name",),
    "States": ("name",),
}

_DATABASE_TABLES = {
    Variant.ACE: ACE_DATABASE_FIELDS,
    Variant.VX: VX_DATABASE_FIELDS,
    Variant.XP: XP_DATABASE_FIELDS,
}


def database_fields(stem: str, variant: Variant) -> tuple[str, ...] | None:
    """The record fields a player reads in ``stem``, or None if it holds none."""
    return _DATABASE_TABLES.get(variant, DATABASE_FIELDS).get(stem)


_SYSTEM_SCALARS = ("gameTitle", "currencyUnit")
_SYSTEM_NAME_LISTS = (
    "armorTypes",
    "elements",
    "equipTypes",
    "skillTypes",
    "weaponTypes",
)
_SYSTEM_TERM_LISTS = ("basic", "commands", "params")

_ACE_SYSTEM_SCALARS = ("game_title", "currency_unit")
_ACE_SYSTEM_NAME_LISTS = ("armor_types", "elements", "skill_types", "weapon_types")
_ACE_SYSTEM_TERM_LISTS = ("basic", "commands", "params", "etypes")

#: The System attribute whose every string is a term, for the generations that keep terms that way.
TERM_OBJECTS = {Variant.XP: "words", Variant.VX: "terms"}

_SYSTEM_NAMES = {
    Variant.ACE: (_ACE_SYSTEM_SCALARS, _ACE_SYSTEM_NAME_LISTS, _ACE_SYSTEM_TERM_LISTS),
    Variant.VX: (("game_title",), ("elements",), ()),
    Variant.XP: ((), ("elements",), ()),
}


def system_names(
    variant: Variant,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(scalars, name_lists, term_lists)`` as ``variant`` spells them."""
    return _SYSTEM_NAMES.get(variant, (_SYSTEM_SCALARS, _SYSTEM_NAME_LISTS, _SYSTEM_TERM_LISTS))


def _event_slots(entries: Any, variant: Variant, key: str) -> list[Slot]:
    """Slots from a list of records that each hold command lists."""
    slots: list[Slot] = []
    for entry in entries or []:
        if not entry:
            continue
        entry_id = entry.get("id")
        if key == "list":
            slots.extend(
                command_slots(entry.get("list") or [], f"ev{entry_id}", variant)
            )
            continue
        for page_index, page in enumerate(entry.get("pages") or []):
            slots.extend(
                command_slots(
                    page.get("list") or [], f"ev{entry_id}:pg{page_index}", variant
                )
            )
    return slots


def _database_slots(records: Any, fields: tuple[str, ...], variant: Variant) -> list[Slot]:
    from bokucho.engines.rpgmaker_language import note_slots

    slots: list[Slot] = []
    for record in records or []:
        if not record:
            continue
        for field in fields:
            text = record.get(field)
            if isinstance(text, str) and text:
                slots.append(
                    Slot(
                        address=f"{record.get('id')}:{field}",
                        kind=field,
                        text=text,
                        container=record,
                        key=field,
                    )
                )
        if variant not in RGSS_VARIANTS:
            localized_fields = (*fields, "message3", "message4") if "message1" in fields else fields
            slots.extend(note_slots(record, localized_fields))
    return slots


def _system_slots(payload: Any, variant: Variant) -> list[Slot]:
    scalars, name_lists, term_lists = system_names(variant)
    slots: list[Slot] = []
    for field in scalars:
        text = payload.get(field)
        if isinstance(text, str) and text:
            slots.append(Slot(field, "term", text, container=payload, key=field))
    for field in name_lists:
        for index, text in enumerate(payload.get(field) or []):
            if isinstance(text, str) and text:
                slots.append(
                    Slot(f"{field}:{index}", "term", text, container=payload[field], key=index)
                )
    name = TERM_OBJECTS.get(variant, "terms")
    terms = payload.get(name)
    if terms is None:
        return slots
    if variant in TERM_OBJECTS:
        slots.extend(
            Slot(f"{name}:{field}", "term", text, container=terms, key=field)
            for field, text in terms.items()
            if isinstance(text, str) and text
        )
        return slots
    for field in term_lists:
        for index, text in enumerate(terms.get(field) or []):
            if isinstance(text, str) and text:
                slots.append(
                    Slot(f"terms:{field}:{index}", "term", text, container=terms[field], key=index)
                )
    messages = terms.get("messages") or {}
    for field, text in messages.items():
        if isinstance(text, str) and text:
            slots.append(
                Slot(f"terms:messages:{field}", "term", text, container=messages, key=field)
            )
    return slots


def file_slots(stem: str, payload: Any, variant: Variant) -> list[Slot]:
    """Every translatable string in one data file, by the file's name."""
    payload = _walkable(payload, variant)
    if stem.startswith("Map") and stem != "MapInfos":
        return _event_slots(payload.get("events"), variant, "pages")
    if stem == "CommonEvents":
        return _event_slots(payload, variant, "list")
    if stem == "Troops":
        return _event_slots(payload, variant, "pages")
    if stem == "System":
        return _system_slots(payload, variant)
    fields = database_fields(stem, variant)
    return _database_slots(payload, fields, variant) if fields else []


_SCRIPTS = "Scripts"


def _script_units(path: Path, roots: tuple[Path, ...]) -> tuple[list[TextUnit], list[str]]:
    """The units of the script text in ``path``, and one warning per script left out.

    ``roots`` are the game's own folder and, for a packed game, where its archive was
    unpacked: an archive holds ``Graphics/`` while ``Audio/`` usually sits beside it.
    """
    from bokucho.engines.rpgmaker_scripts import load_scripts, script_texts

    file = load_scripts(path.read_bytes())
    assets = {
        found.stem: found.relative_to(root).as_posix()
        for root in dict.fromkeys(roots)
        for folder in ("Graphics", "Audio")
        for found in sorted((root / folder).rglob("*"))
        if found.is_file()
    }
    texts, problems = script_texts(file, assets)
    units = []
    for text in texts:
        reference = {
            "file": _SCRIPTS,
            "address": text.address,
            "kind": "script",
            "script": text.script,
            "key": text.key,
        }
        if text.asset is not None:
            reference["asset"] = text.asset
        units.append(
            TextUnit(
                id=f"{_SCRIPTS}:{text.address}",
                source=text.text,
                status=text.status,
                kind="script",
                engine_ref=reference,
                context=Context(scene=f"{text.script}:{text.line}"),
            )
        )
    return units, problems


class RpgMakerAdapter(EngineAdapter):
    """Adapter for RPG Maker MV, MZ, XP, VX, and VX Ace."""

    name = "rpgmaker"

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace) if workspace is not None else None

    def _unpack(
        self,
        layout: Layout,
        sink: ProgressSink,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> Layout:
        """A layout whose data directory exists, unpacking the archive if not."""
        if layout.archive is None:
            return layout
        if self.workspace is None:
            raise NotRpgMaker(
                f"{layout.archive.name} has not been unpacked, and this adapter "
                "was built without a workspace to unpack it into."
            )
        destination = (self.workspace / layout.archive.stem).resolve()
        unpacked = Layout(layout.variant, destination, destination / "Data", None)
        digest = archive_digest(layout.archive)
        if holds_original(destination, layout.archive, digest) and unpacked.data_dir.is_dir():
            return unpacked

        staging = (self.workspace / f"{layout.archive.stem}.partial").resolve()
        planned = _planned_entries(layout.archive, staging)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        promoted = False
        try:
            _write_entries(layout.archive, planned, sink, token)
            if destination.exists():
                shutil.rmtree(destination)
            staging.replace(destination)
            promoted = True
            unpacked_stamp(destination).write_text(digest, encoding="ascii")
        finally:
            if not promoted and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return unpacked

    @classmethod
    def game_root(cls, game_path: Path) -> Path | None:
        """The tree the game lives in, which for MV and MZ holds the launcher."""
        try:
            layout = sniff(game_path)
        except NotRpgMaker:
            return None
        return None if layout.archive is not None else layout.root

    @classmethod
    def packed_archive(cls, game_path: Path) -> Path:
        """The archive the game unpacks from, or ``game_path`` when it holds no archive."""
        try:
            return sniff(game_path).archive or game_path
        except NotRpgMaker:
            return game_path

    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        from bokucho.engines.rpgmaker_language import PLUGIN_STEM, load_registry, plugin_slots

        sniffed = sniff(Path(game_path))
        layout = self._unpack(sniffed, sink, token)
        paths = sorted(layout.data_dir.glob(f"*{DATA_SUFFIX[layout.variant]}"))
        registry = layout.data_dir.parent / "js" / "plugins.js"
        if layout.variant is not Variant.ACE and registry.is_file():
            paths.append(registry)

        def unreadable(index: int, detail: str) -> None:
            sink.publish(
                ProgressEvent(
                    "extract",
                    index,
                    len(paths),
                    "extract.file.unreadable",
                    level=ProgressLevel.WARNING,
                    detail=detail,
                )
            )

        units: list[TextUnit] = []
        for index, path in enumerate(paths, start=1):
            token.raise_if_cancelled()
            stem = PLUGIN_STEM if path == registry else path.stem
            found: list[TextUnit] = []
            problems: list[str] = []
            try:
                if path == registry:
                    slots = plugin_slots(load_registry(path)[1])
                elif stem == _SCRIPTS and layout.variant in RGSS_VARIANTS:
                    slots = []
                    found, problems = _script_units(path, (sniffed.root, layout.root))
                else:
                    slots = file_slots(stem, load_data(path), layout.variant)
            except _UNREADABLE_ERRORS as exc:
                unreadable(index, f"{path.name}: {exc}")
                continue
            for problem in problems:
                unreadable(index, problem)
            units.extend(found)
            units.extend(_slot_unit(stem, slot) for slot in slots)
            sink.publish(
                ProgressEvent("extract", index, len(paths), "extract.file.read", detail=path.name)
            )
        return units

    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        from bokucho.engines.rpgmaker_language import PLUGIN_STEM
        from bokucho.engines.rpgmaker_scripts import ScriptRefused, rewrite_scripts

        layout = self._unpack(sniff(Path(game_path)), sink, token)
        scripts_variant = layout.variant in RGSS_VARIANTS
        wanted = _targets_by_file(units)
        destination = Path(out_path) / layout.relative_data
        prepared: list[tuple[Path, Any]] = []
        mismatches: list[str] = []
        refusals: list[str] = []
        for index, (stem, targets) in enumerate(sorted(wanted.items()), start=1):
            token.raise_if_cancelled()
            is_registry = stem == PLUGIN_STEM and layout.variant is not Variant.ACE
            is_scripts = stem == _SCRIPTS and scripts_variant
            source = (
                layout.data_dir.parent / "js" / "plugins.js"
                if is_registry else layout.data_dir / f"{stem}{DATA_SUFFIX[layout.variant]}"
            )
            if not source.is_file():
                continue
            try:
                payload, slots = _load_for_inject(source, stem, layout, is_registry, is_scripts)
            except _UNREADABLE_ERRORS as exc:
                sink.publish(
                    ProgressEvent(
                        "inject",
                        index,
                        len(wanted),
                        "inject.file.unreadable",
                        level=ProgressLevel.WARNING,
                        detail=f"{source.name}: {exc}",
                    )
                )
                continue
            if is_scripts:
                payload, refused = rewrite_scripts(payload, targets)
                refusals.extend(refused)
            mismatches.extend(_write_slots(stem, slots, targets))
            folder = destination.parent / "js" if is_registry else destination
            prepared.append((folder / source.name, payload))

        if refusals:
            raise ScriptRefused("\n".join([*refusals, *mismatches]))
        if mismatches:
            raise LineCountMismatch("\n".join(mismatches))
        for index, (path, payload) in enumerate(prepared, start=1):
            token.raise_if_cancelled()
            _write_injected(path, payload, scripts_variant)
            sink.publish(
                ProgressEvent(
                    "inject", index, len(prepared), "inject.file.written", detail=path.name
                )
            )


def _targets_by_file(units: list[TextUnit]) -> dict[str, dict[str, tuple[str, str]]]:
    """Each translated unit's source and target, by the file and address it came from."""
    wanted: dict[str, dict[str, tuple[str, str]]] = {}
    for unit in units:
        if unit.target:
            reference = unit.engine_ref
            wanted.setdefault(str(reference.get("file")), {})[
                str(reference.get("address"))
            ] = (unit.source, unit.target)
    return wanted


def _load_for_inject(
    source: Path, stem: str, layout: Layout, is_registry: bool, is_scripts: bool
) -> tuple[Any, list]:
    from bokucho.engines.rpgmaker_language import load_registry, plugin_slots
    from bokucho.engines.rpgmaker_scripts import load_scripts

    if is_registry:
        payload = load_registry(source)
        return payload, plugin_slots(payload[1])
    if is_scripts:
        return load_scripts(source.read_bytes()), []
    payload = load_data(source)
    return payload, file_slots(stem, payload, layout.variant)


def _write_slots(stem: str, slots: list, targets: dict[str, tuple[str, str]]) -> list[str]:
    """Write each slot's translation; the slots whose line count would change, as messages."""
    mismatches = []
    for slot in slots:
        if slot.address in targets:
            try:
                slot.write(targets[slot.address][1])
            except (LineCountMismatch, ValueError) as exc:
                mismatches.append(f"{stem}: {exc}")
    return mismatches


def _write_injected(path: Path, payload: Any, scripts_variant: bool) -> None:
    from bokucho.engines.rpgmaker_language import dump_registry

    if path.suffix == ".js":
        text = dump_registry(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
    elif path.stem == _SCRIPTS and scripts_variant:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    else:
        write_data(path, payload)


def _planned_entries(archive: Path, staging: Path) -> list[tuple[Any, Path, Path]]:
    """Each archive entry, where it unpacks to under ``staging``, and its path inside."""
    from bokucho.engines import rgssad

    planned = []
    for entry in rgssad.entries(archive):
        relative = Path(entry.name.replace("\\", "/"))
        target = (staging / relative).resolve()
        if not target.is_relative_to(staging):
            raise ValueError(
                f"{archive.name} names an entry outside its own tree: {entry.name}"
            )
        planned.append((entry, target, relative))
    return planned


def _write_entries(
    archive: Path,
    planned: list[tuple[Any, Path, Path]],
    sink: ProgressSink,
    token: CancellationToken,
) -> None:
    """Write each entry it can read; one it cannot is reported and left out."""
    from bokucho.engines import rgssad

    for index, (entry, target, relative) in enumerate(planned, start=1):
        token.raise_if_cancelled()
        try:
            payload = rgssad.read(archive, entry)
        except rgssad.EntryUnreadable as exc:
            sink.publish(
                ProgressEvent(
                    "engine.unpack", index, len(planned),
                    "unpack.file.unreadable",
                    level=ProgressLevel.WARNING,
                    detail=f"{relative.name}: {exc}",
                )
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        sink.publish(
            ProgressEvent(
                "engine.unpack", index, len(planned), "unpack.file.written",
                detail=relative.name,
            )
        )


def _slot_unit(stem: str, slot) -> TextUnit:
    return TextUnit(
        id=f"{stem}:{slot.address}",
        source=slot.text,
        status=Status.UNTRANSLATED,
        engine_ref={"file": stem, "address": slot.address, "kind": slot.kind},
        context=Context(speaker=slot.speaker or None, tags=list(slot.tags)),
    )
