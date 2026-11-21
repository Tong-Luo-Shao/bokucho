"""Publish translations through an engine's own loader: RPG Maker MV/MZ's plugins, Ren'Py's tl,
and BepInEx with XUnity.AutoTranslator for Unity."""

from __future__ import annotations

import json
import re
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

from bokucho.application.errors import InvalidProject
from bokucho.application.projects import injectable_units, resolve_game_path
from bokucho.engines.rpgmaker import RGSS_VARIANTS, file_slots, load_data, sniff
from bokucho.engines.rpgmaker_language import PLUGIN_STEM
from bokucho.model import TextUnit
from bokucho.patcher.payload import safe_parts
from bokucho.project import Project, unpacked_game_dir


def _locations(value: Any, path: tuple = ()) -> dict[int, tuple]:
    if not isinstance(value, (dict, list)):
        return {}
    found = {id(value): path}
    entries = enumerate(value) if isinstance(value, list) else value.items()
    for key, child in entries:
        found.update(_locations(child, (*path, key)))
    return found


def write_rpgmaker_mount(project: Project, destination: Path) -> bool:
    """Return false for XP, VX and VX Ace, whose RGSS games have no JS plugin loader."""
    layout = sniff(resolve_game_path(project))
    if layout.variant in RGSS_VARIANTS:
        return False
    checks = {layout.data_dir / "System.json"}
    translations = {}
    for stem, units in sorted(_mounted_units(project).items()):
        source = layout.data_dir / f"{stem}.json"
        if not source.is_file():
            raise InvalidProject(f"Translation source no longer exists: {source}")
        checks.add(source)
        translations[f"{stem}.json"] = _changes(stem, units, load_data(source), layout.variant)
    base = layout.data_dir.parent.relative_to(layout.root)
    pictures = _copy_pictures(project.root / "out", base, destination)
    template = Path(__file__).resolve().parents[1] / "resources" / "patches" / "rpgmaker.js"
    script = template.read_text(encoding="utf-8")
    values = {"__BOKUCHO_TRANSLATIONS__": translations, "__BOKUCHO_PICTURES__": pictures}
    script = re.sub(
        r"__BOKUCHO_(?:TRANSLATIONS|PICTURES)__",
        lambda match: json.dumps(values[match[0]], ensure_ascii=True),
        script,
    )
    target = destination / base / "js" / "plugins" / "Bokucho.js"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script, encoding="utf-8")
    metadata = {
        "registry": (base / "js" / "plugins.js").as_posix(),
        "checks": {
            path.relative_to(layout.root).as_posix(): sha256(path.read_bytes()).hexdigest()
            for path in sorted(checks)
        },
    }
    (destination / "mount.json").write_text(json.dumps(metadata), encoding="utf-8")
    return True


def _mounted_units(project: Project) -> dict[str, dict[str, TextUnit]]:
    """The translated units by data file and address; a plugin parameter cannot be mounted."""
    wanted: dict[str, dict[str, TextUnit]] = {}
    for unit in injectable_units(project):
        if unit.target and unit.kind != "image":
            stem = str(unit.engine_ref.get("file", ""))
            if stem == PLUGIN_STEM:
                raise InvalidProject(
                    "LL_LanguageOption parameters require `bokucho inject` and copying out/ "
                    "to a game copy; mounted patches cannot change startup parameters yet."
                )
            if len(safe_parts(stem)) != 1:
                raise InvalidProject(f"Invalid RPG Maker file reference: {stem}")
            wanted.setdefault(stem, {})[str(unit.engine_ref.get("address"))] = unit
    return wanted


def _changes(stem: str, units: dict[str, TextUnit], payload: Any, variant) -> list[list[dict]]:
    """For each translated slot, the JSON paths the plugin rewrites, from and to what."""
    locations = _locations(payload)
    slots = file_slots(stem, payload, variant)
    if units.keys() - {slot.address for slot in slots}:
        raise InvalidProject(f"{stem} has stale translation anchors; extract the text again.")
    groups = []
    for slot in slots:
        unit = units.get(slot.address)
        if unit is None:
            continue
        if unit.source != slot.text:
            raise InvalidProject(f"{stem}:{slot.address} changed; extract the text again.")
        if slot.commands is not None:
            cells = [(command["parameters"], 0) for command in slot.commands]
        else:
            cells = [(slot.container, slot.key)]
        previous = [parent[key] for parent, key in cells]
        slot.write(unit.target)
        changes = [
            {"path": [*locations[id(parent)], key], "source": source, "target": parent[key]}
            for (parent, key), source in zip(cells, previous, strict=True)
            if parent[key] != source
        ]
        if changes:
            groups.append(changes)
    return groups


def _copy_pictures(overlay: Path, base: Path, destination: Path) -> dict[str, bool]:
    """Copy the translated pictures beside the plugin; the names it swaps in."""
    pictures = {}
    for path in sorted((overlay / base / "img").rglob("*")):
        if path.is_file() and path.suffix.lower() in {".png", ".rpgmvp", ".png_"}:
            if not path.resolve().is_relative_to(overlay.resolve()):
                raise InvalidProject(f"Picture escapes the overlay: {path}")
            relative = path.relative_to(overlay / base)
            safe_parts(relative.as_posix())
            target = destination / base / "bokucho" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            pictures[relative.with_suffix("").as_posix()] = True
    return pictures


def _copy_textures(overlay: Path, originals: Path, destination: Path) -> bool:
    """Copy each translated texture under the name XUnity loads it by; whether any was."""
    from bokucho.engines.unity import translated_texture_name

    copied = False
    for original in sorted(originals.glob("*.png")):
        translated = overlay / original.name
        if not translated.is_file():
            continue
        data = translated.read_bytes()
        name = translated_texture_name(original.name, original.read_bytes(), data)
        if name is None:
            continue
        destination.mkdir(parents=True, exist_ok=True)
        (destination / name).write_bytes(data)
        copied = True
    return copied


def write_renpy_mount(project: Project, destination: Path) -> None:
    """The filled ``game/tl/<language>/`` scripts, and the script that selects that language."""
    from bokucho.engines.renpy import RenpyAdapter, default_language
    from bokucho.patcher.renpy_mount import LANGUAGE, LANGUAGE_SCRIPT, language_script

    language = project.config.engine.language or default_language(project.config.target_lang)
    if not LANGUAGE.fullmatch(language):
        raise InvalidProject(f"Ren'Py language names a folder and cannot be {language!r}.")
    RenpyAdapter(language=language).inject(
        injectable_units(project), resolve_game_path(project), destination
    )
    script = destination.joinpath(*safe_parts(LANGUAGE_SCRIPT))
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(language_script(language), encoding="utf-8")
    (destination / "mount.json").write_text(
        json.dumps({"kind": "renpy", "language": language}), encoding="utf-8"
    )


def write_cs2_mount(project: Project, destination: Path) -> None:
    """The filled ``game/data/localization/*.json`` the CatSystem2 loader reads at runtime."""
    from bokucho.engines.cs2 import Cs2Adapter

    Cs2Adapter(
        source_lang=project.config.source_lang,
        target_lang=project.config.target_lang,
        prefill_lang=project.config.engine.prefill_lang,
    ).inject(injectable_units(project), resolve_game_path(project), destination)
    overlays = sorted((destination / "game" / "data" / "localization").glob("*.json"))
    if not overlays:
        raise InvalidProject("CS2 injection produced no localization overlay to mount.")
    (destination / "mount.json").write_text(json.dumps({"kind": "cs2"}), encoding="utf-8")


def write_unity_mount(project: Project, destination: Path, fonts: Path) -> None:
    """The translation XUnity.AutoTranslator reads, with a fallback font from ``fonts``.

    The patcher fetches BepInEx and XUnity.AutoTranslator for the player's game.
    """
    from bokucho.engines.unity import (
        FONT_DIR,
        UnityAdapter,
        UnityLayout,
        font_bundle,
        font_generation,
        xunity_language,
    )
    from bokucho.patcher.unity_loader import architecture, il2cpp_loadable

    game = resolve_game_path(project)
    layout = UnityLayout(game)
    player = layout.root / "UnityPlayer.dll"
    if not player.is_file():
        raise InvalidProject(
            f"{layout.root} has no UnityPlayer.dll. Older Unity versions build the player "
            "into the game's program, and the BepInEx loader a Unity patch installs cannot "
            "load into such a game; its text can be extracted and translated, not patched."
        )
    try:
        architecture(player)
    except ValueError as error:
        raise InvalidProject(str(error)) from error
    if layout.il2cpp and not il2cpp_loadable(layout.version()):
        raise InvalidProject(
            f"No BepInEx build loads an IL2CPP game built with Unity {layout.version()} yet; "
            "its text can be extracted and translated, not patched."
        )
    fallback = None
    generation = font_generation(layout.version()) if layout.uses_text_mesh_pro() else None
    font = fonts / font_bundle(generation) if generation else None
    # shortcut: a Unity year with no font built (before 2018) ships without one, so
    # TextMesh Pro may still draw boxes there; build that year's font to cover it.
    if font is not None and font.is_file():
        target = destination.joinpath(*FONT_DIR.split("/"), font.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(font, target)
        fallback = f"{FONT_DIR}/{font.name}"
    # XUnity's TextureDirectory, Translation\{Lang}\Texture under BepInEx.
    language = xunity_language(project.config.target_lang)
    textures = _copy_textures(
        project.root / "out",
        unpacked_game_dir(project.root, layout.data),
        destination / "BepInEx" / "Translation" / language / "Texture",
    )
    UnityAdapter(
        project.config.source_lang,
        project.config.target_lang,
        fallback_font=fallback,
        textures=textures,
    ).inject(injectable_units(project), game, destination)
    metadata = {"kind": "unity", "data": layout.data.name}
    if layout.il2cpp:
        metadata["backend"] = "il2cpp"
    (destination / "mount.json").write_text(json.dumps(metadata), encoding="utf-8")
