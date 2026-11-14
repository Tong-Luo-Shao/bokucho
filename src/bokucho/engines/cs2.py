"""CatSystem2-on-Ren'Py adapter.

These games run on Ren'Py but their story is not Ren'Py dialogue: the compiled
``story/*.rpyc`` carry no text, and every line is pulled at runtime, by scene
and line index, from ``game/data/localization/<lang>.json``. The Ren'Py SDK's
``translate`` command therefore sees none of it, so this adapter reads those
localization files directly instead.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import re
import zlib
from pathlib import Path

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject
from bokucho.engines.base import EngineAdapter
from bokucho.engines.renpy_archive import read_member, rpa_index
from bokucho.model import Status, TextUnit

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()

_LOC_DIR = "data/localization"


def _stem(lang: str) -> str:
    """A localization filename stem from a language tag (``zh-cn`` -> ``zh_cn``)."""
    return re.sub(r"[^a-z0-9]+", "_", lang.lower()).strip("_")


def _game_dir(game_path: Path) -> Path:
    """The folder holding ``game/``; the desktop app may pick a game by its .exe."""
    folder = game_path.parent if game_path.is_file() else game_path
    if not (folder / "game").is_dir():
        raise InvalidProject(
            f"{folder} is not a Ren'Py/CS2 game: it has no game/ folder. "
            "Point game_path at the game's folder or its .exe."
        )
    return folder


def _localization(game_dir: Path, stem: str, *, required: bool = True) -> dict:
    """One ``data/localization/<stem>.json``, loose in game/ or inside a game/*.rpa."""
    rel = f"{_LOC_DIR}/{stem}.json"
    loose = game_dir / "game" / "data" / "localization" / f"{stem}.json"
    if loose.is_file():
        return json.loads(loose.read_text(encoding="utf-8"))
    for archive in sorted((game_dir / "game").glob("*.rpa")):
        try:
            index = rpa_index(archive)
        except (OSError, ValueError, zlib.error, pickle.UnpicklingError):
            continue
        if rel in index:
            return json.loads(read_member(archive, rel, index).decode("utf-8"))
    if not required:
        return {}
    raise InvalidProject(
        f"CS2 localization '{rel}' not found under {game_dir / 'game'} "
        "(looked for a loose file and inside game/*.rpa)."
    )


def _line_order(mapping: dict) -> list[str]:
    """Scene line keys in natural order: numeric indices as numbers, else by text."""
    return sorted(mapping, key=lambda k: (0, int(k)) if k.isdigit() else (1, k))


_RUBY = re.compile(r"\[([^\[\]/]+)/[^\[\]]+\]")


def _strip_ruby(text: str) -> str:
    """CatSystem2 furigana ``[base/reading]`` -> ``base``.

    ponytail: the reading is a Japanese pronunciation aid with no place in a
    Chinese line. Left in, the translator either romanizes it ([一/ichi]) or
    translates both halves ([公園/這裡]); dropping it to the base kanji before
    translation is what the 38 ruby lines want (30 already read best that way).
    """
    return _RUBY.sub(r"\1", text)


def _unit(uid: str, file: str, section: str, source: str, target: str, ref: dict) -> TextUnit:
    source, target = _strip_ruby(source), _strip_ruby(target)
    filled = bool(target and target.strip())
    return TextUnit(
        id=uid,
        source=source,
        target=target if filled else "",
        status=Status.MT if filled else Status.UNTRANSLATED,
        engine_ref={"kind": "cs2", "section": section, "file": file, **ref},
    )


#: CatSystem2's line-break code, opening every line after a message's first.
_BREAK = "\\n"


def _messages(lines: dict, ranges: dict) -> list[list[str]]:
    """Each message as its line keys, the lines of one dialogue range together.

    The game breaks a long message over several text keys and names them in
    ``dialogue_ranges`` (head -> exclusive end). Sent one key at a time, the tail of
    a sentence is translated on its own and comes back as nonsense.
    """
    messages: list[list[str]] = []
    taken: set[str] = set()
    for key in _line_order(lines):
        if key in taken:
            continue
        end = ranges.get(key)
        keys = [key]
        if isinstance(end, int) and key.isdigit():
            keys = [str(n) for n in range(int(key), end)]
        if not all(k in lines and lines[k].startswith(_BREAK) for k in keys[1:]):
            keys = [key]  # not the shape seen in any game; leave such lines apart
        taken.update(keys)
        messages.append(keys)
    return messages


def _joined(lines: list[str]) -> str:
    """One message's lines as one text, each ``\\n`` code a real line break."""
    return "\n".join([lines[0], *(line.removeprefix(_BREAK) for line in lines[1:])])


def _cut(target: str, count: int) -> list[str]:
    """A message's translation back as its ``count`` lines, ``\\n`` reopening each.

    Lines past the last fold into it and missing ones are left blank: the shipped
    zh_cn leaves 64 continuation lines blank and the game shows those messages whole.
    """
    pieces = target.split("\n")
    pieces = [*pieces[: count - 1], "".join(pieces[count - 1 :])]
    pieces += [""] * (count - len(pieces))
    return [pieces[0], *(_BREAK + piece if piece else "" for piece in pieces[1:])]


def units_from_localization(source: dict, prefill: dict) -> list[TextUnit]:
    """TextUnits for one CS2 game: ``source`` is the original, ``prefill`` the draft."""
    units: list[TextUnit] = []

    stext = source.get("text") or {}
    ptext = prefill.get("text") or {}
    ranges = source.get("dialogue_ranges") or {}
    for scene in sorted(stext):
        lines = stext[scene] or {}
        drafted = ptext.get(scene) or {}
        for keys in _messages(lines, ranges.get(scene) or {}):
            if len(keys) == 1:
                (line,) = keys
                uid, ref = f"text/{scene}/{line}", {"scene": scene, "line": line}
            else:
                # The id is new, so extracting again stitches the lines' old translations.
                uid = f"text/{scene}/{keys[0]}-{keys[-1]}"
                parts: list[dict] = [{"block": f"text/{scene}/{keys[0]}"}]
                for key in keys[1:]:
                    parts += [{"text": "\n"}, {"block": f"text/{scene}/{key}", "drop": _BREAK}]
                ref = {"scene": scene, "lines": keys, "parts": parts}
            units.append(
                _unit(
                    uid,
                    f"text/{scene}",
                    "text",
                    _joined([lines[key] for key in keys]),
                    _joined([drafted.get(key, "") for key in keys]),
                    ref,
                )
            )

    # ui / choices: identifier -> on-screen text.
    for section in ("ui", "choices"):
        smap = source.get(section) or {}
        pmap = prefill.get(section) or {}
        for key in smap:
            units.append(
                _unit(
                    f"{section}/{key}", section, section, smap[key], pmap.get(key, ""), {"key": key}
                )
            )

    # names: the key is the canonical source name; each language's value is its display.
    snames = source.get("names") or {}
    pnames = prefill.get("names") or {}
    for key in dict.fromkeys([*snames, *pnames]):
        digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
        units.append(
            _unit(
                f"names/{digest}",
                "names",
                "names",
                snames.get(key) or key,
                pnames.get(key, ""),
                {"key": key},
            )
        )
    return units


def units_from_system_prompts(sp: dict, source_stem: str, prefill_stem: str) -> list[TextUnit]:
    """TextUnits for ``system_prompts.json``: system messages and save-slot chapter titles.

    Each entry inlines every language by id, so the source is the ``source_stem`` value
    (the Japanese line) and the draft is the ``prefill_stem`` value (the shipped zh_cn).
    """
    units: list[TextUnit] = []
    for group in ("messages", "save_titles"):
        for entry in sp.get(group) or []:
            if not isinstance(entry, dict):
                continue
            uid = entry.get("id")
            source = entry.get(source_stem) or entry.get("source") or ""
            if not uid or not source:
                continue
            target = entry.get(prefill_stem, "") if prefill_stem else ""
            units.append(
                _unit(
                    f"system_prompts/{group}/{uid}",
                    "system_prompts",
                    "system_prompts",
                    source,
                    target,
                    {"group": group, "id": uid},
                )
            )
    return units


def units_from_extras_ui(eu: dict, source_stem: str, prefill_stem: str) -> list[TextUnit]:
    """TextUnits for ``extras_ui.json`` (gallery/music UI), keyed ``{lang: {key: text}}``."""
    smap = eu.get(source_stem) or {}
    pmap = (eu.get(prefill_stem) or {}) if prefill_stem else {}
    return [
        _unit(
            f"extras_ui/{key}", "extras_ui", "extras_ui", smap[key], pmap.get(key, ""), {"key": key}
        )
        for key in smap
    ]


def apply_units_to_localization(loc: dict, units: list[TextUnit]) -> int:
    """Overlay each translated unit's target onto ``loc`` in place; return how many."""
    applied = 0
    for unit in units:
        if not (unit.target and unit.target.strip()):
            continue
        ref = unit.engine_ref
        section = ref.get("section")
        if section == "text":
            scene = loc.setdefault("text", {}).setdefault(ref["scene"], {})
            if "lines" in ref:
                scene.update(zip(ref["lines"], _cut(unit.target, len(ref["lines"])), strict=True))
            else:
                scene[ref["line"]] = unit.target
        elif section in ("ui", "choices", "names"):
            loc.setdefault(section, {})[ref["key"]] = unit.target
        else:
            continue
        applied += 1
    return applied


def apply_units_to_system_prompts(sp: dict, units: list[TextUnit], lang: str) -> int:
    """Overwrite the ``lang`` value of each matching entry by (group, id); return how many."""
    index = {
        (group, entry["id"]): entry
        for group in ("messages", "save_titles")
        for entry in sp.get(group) or []
        if isinstance(entry, dict) and entry.get("id")
    }
    applied = 0
    for unit in units:
        ref = unit.engine_ref
        if ref.get("section") != "system_prompts" or not (unit.target and unit.target.strip()):
            continue
        entry = index.get((ref.get("group"), ref.get("id")))
        if entry is not None:
            entry[lang] = unit.target
            applied += 1
    return applied


def apply_units_to_extras_ui(eu: dict, units: list[TextUnit], lang: str) -> int:
    """Overwrite ``eu[lang][key]`` for each translated extras_ui unit; return how many."""
    target_map = eu.setdefault(lang, {})
    applied = 0
    for unit in units:
        ref = unit.engine_ref
        if ref.get("section") != "extras_ui" or not (unit.target and unit.target.strip()):
            continue
        target_map[ref["key"]] = unit.target
        applied += 1
    return applied


class Cs2Adapter(EngineAdapter):
    """Adapter for CatSystem2 games ported to Ren'Py (JSON localization layer)."""

    name = "cs2"

    def __init__(
        self, source_lang: str = "", target_lang: str = "", prefill_lang: str = ""
    ) -> None:
        self._source_stem = _stem(source_lang) or "ja"
        self._prefill_stem = _stem(prefill_lang)
        self.target_lang = target_lang

    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        del sink, token
        game_dir = _game_dir(game_path)
        source = _localization(game_dir, self._source_stem)
        prefill = (
            _localization(game_dir, self._prefill_stem, required=False)
            if self._prefill_stem and self._prefill_stem != self._source_stem
            else {}
        )
        units = units_from_localization(source, prefill)
        system_prompts = _localization(game_dir, "system_prompts", required=False)
        if system_prompts:
            units += units_from_system_prompts(
                system_prompts, self._source_stem, self._prefill_stem
            )
        extras_ui = _localization(game_dir, "extras_ui", required=False)
        if extras_ui:
            units += units_from_extras_ui(extras_ui, self._source_stem, self._prefill_stem)
        if not units:
            raise InvalidProject(
                f"{self._source_stem}.json under {game_dir / 'game'} held no CS2 text."
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
        del sink, token
        game_dir = _game_dir(game_path)
        # Overwrite the slot the game already loads and displays (the drafted layer,
        # e.g. zh_cn) rather than adding a new language the selector may never pick.
        slot = self._prefill_stem or _stem(self.target_lang)
        if not slot or slot == self._source_stem:
            raise InvalidProject(
                "CS2 injection needs a target localization slot to overwrite: set "
                "[engine] prefill_lang (the shipped translation to replace, e.g. zh_cn) "
                "or a target_lang distinct from the source."
            )
        # Start from the slot's own content so structure, dialogue_ranges and any
        # untranslated lines survive; fall back to the source layer if it is absent.
        base = _localization(game_dir, slot, required=False) or _localization(
            game_dir, self._source_stem
        )
        apply_units_to_localization(base, units)
        self._write_overlay(out_path, slot, base)

        # system_prompts.json and extras_ui.json inline every language in one file, so
        # overwrite the slot's values there too and emit each as its own overlay.
        system_prompts = _localization(game_dir, "system_prompts", required=False)
        if system_prompts and apply_units_to_system_prompts(system_prompts, units, slot):
            self._write_overlay(out_path, "system_prompts", system_prompts)
        extras_ui = _localization(game_dir, "extras_ui", required=False)
        if extras_ui and apply_units_to_extras_ui(extras_ui, units, slot):
            self._write_overlay(out_path, "extras_ui", extras_ui)

    @staticmethod
    def _write_overlay(out_path: Path, stem: str, payload: dict) -> None:
        dest = out_path / "game" / _LOC_DIR / f"{stem}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
