"""Text fields owned by LL_LanguageOption, including its JSON-encoded parameters."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bokucho.engines.rpgmaker import RpgMakerWriteError, Slot

PLUGIN_STEM = "Plugins"
_PLUGIN = "LL_LanguageOption"
_NOTE_TAG = re.compile(r"<([^<>:]+):([^>]*)>")
_ASSIGNMENT = re.compile(r"^[\t \ufeff]*(?:(?:var|let|const)\s+)?\$plugins\s*=\s*", re.M)


@dataclass
class _NoteSlot(Slot):
    tag: str = ""

    def write(self, target: str) -> None:
        if ">" in target:
            raise RpgMakerWriteError(f"{self.address}: a note translation cannot contain '>'")
        note = self.container[self.key]
        match = next(
            match for match in reversed(list(_NOTE_TAG.finditer(note))) if match[1] == self.tag
        )
        self.container[self.key] = note[: match.start(2)] + target + note[match.end(2) :]


def note_slots(record: Any, fields: tuple[str, ...]) -> list[Slot]:
    """Read localized text tags, leaving all other note content alone."""
    note = record.get("note")
    if not isinstance(note, str):
        return []
    tags = {match[1]: match[2] for match in _NOTE_TAG.finditer(note)}
    slots = []
    for tag, text in tags.items():
        field, _, language = tag.rpartition("_")
        if field not in fields or not language.isascii() or not language.isdecimal() or not text:
            continue
        slots.append(
            _NoteSlot(
                address=f"{record.get('id')}:note:{tag}",
                kind=field,
                text=text,
                container=record,
                key="note",
                tag=tag,
                tags=(f"language:{language}",),
            )
        )
    return slots


def load_registry(path: Path) -> tuple[str, list[dict], str]:
    """Decode only the registry array; never execute the surrounding JavaScript."""
    with path.open(encoding="utf-8", newline="") as stream:
        text = stream.read()
    assignment = _ASSIGNMENT.search(text)
    if assignment is None:
        raise ValueError("No $plugins JSON array found")
    plugins, end = json.JSONDecoder().raw_decode(text, assignment.end())
    if not isinstance(plugins, list) or not all(isinstance(plugin, dict) for plugin in plugins):
        raise ValueError("The $plugins registry must be an array of objects")
    return text[: assignment.end()], plugins, text[end:]


def dump_registry(registry: tuple[str, list[dict], str]) -> str:
    prefix, plugins, suffix = registry
    return prefix + json.dumps(plugins, ensure_ascii=True, separators=(",", ":")) + suffix


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) and value else value


def _replace(value: Any, path: tuple[str | int, ...], target: str) -> Any:
    if not path:
        return target
    if isinstance(value, str):
        return json.dumps(_replace(json.loads(value), path, target), ensure_ascii=False)
    key, *remaining = path
    value[key] = _replace(value[key], tuple(remaining), target)
    return value


@dataclass
class _ParameterSlot(Slot):
    path: tuple[str | int, ...] = ()

    def write(self, target: str) -> None:
        self.container[self.key] = _replace(self.container[self.key], self.path, target)


def plugin_slots(plugins: list[dict]) -> list[Slot]:
    """Whitelist displayed language settings; image names and other plugins are not text."""
    plugin = next(
        (
            plugin
            for plugin in plugins
            if plugin.get("name") == _PLUGIN and plugin.get("status") is True
        ),
        None,
    )
    if plugin is None:
        return []
    parameters = plugin.get("parameters") or {}
    languages = _decode(parameters.get("languages"))
    if not isinstance(languages, list):
        return []
    slots: list[Slot] = []
    for index, entry in enumerate(languages):
        language = _decode(entry)
        if not isinstance(language, dict):
            continue
        tags = (_PLUGIN, f"language:{index}", str(language.get("name", "")))
        slots += [
            _ParameterSlot(
                address=f"{_PLUGIN}:languages:{index}:" + ":".join(map(str, path)),
                kind="term",
                text=text,
                container=parameters,
                key="languages",
                path=(index, *path),
                tags=tags,
            )
            for path, text in _language_texts(language)
            if isinstance(text, str) and text
        ]
    return slots


def _language_texts(language: dict) -> Iterator[tuple[tuple[str | int, ...], Any]]:
    """Each value of one language's settings that may be shown, with its path in it."""
    for field in ("name", "currencyUnit", "titleText"):
        yield (field,), language.get(field)
    for field in ("termBasics", "termParams", "termCommands", "termMessages"):
        terms = _decode(language.get(field))
        if isinstance(terms, dict):
            for key, text in terms.items():
                yield (field, key), text
    types = _decode(language.get("types"))
    if isinstance(types, dict):
        for field in ("elements", "skillTypes", "equipTypes"):
            names = _decode(types.get(field))
            if isinstance(names, list):
                for position, text in enumerate(names):
                    yield ("types", field, position), text


