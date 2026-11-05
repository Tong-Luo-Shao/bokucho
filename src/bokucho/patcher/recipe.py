"""Compile translation data in a disposable workspace, without changing the game."""

from __future__ import annotations

import io
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from bokucho.application.fonts import patch_game_font
from bokucho.application.projects import inject_translations
from bokucho.application.proxy_font import UIF_CONFIG, UIF_DLL, patch_system_font, uif_config
from bokucho.engines.base import NoPixelFont
from bokucho.engines.livemaker import LiveMakerAdapter
from bokucho.patcher.payload import Patch, PatchAdded, safe_parts
from bokucho.project import EngineConfig, FontConfig, Project, ProjectConfig

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gal", ".gif"})
RGSS_SUFFIXES: dict[str, str] = {
    "rpgmaker-xp": ".rxdata",
    "rpgmaker-vx": ".rvdata",
    "rpgmaker-vx-ace": ".rvdata2",
}
FONT_LICENSE = "font-license.txt"
#: What a font fill names, as the injected Ruby loader quotes it.
_FILL_FILE = re.compile(r"Bokucho-[A-Za-z0-9]+\.ttf")
_FILL_FAMILY = re.compile(r"Bokucho [A-Za-z0-9 ]+")


def recipe_size(data: bytes) -> int:
    with ZipFile(io.BytesIO(data)) as archive:
        return sum(info.file_size for info in archive.infolist())


def compile_recipe(
    folder: Path,
    patch: Patch,
    root: Path,
    log: Callable[[str], None] | None = None,
) -> tuple[dict[str, Path], tuple[PatchAdded, ...]]:
    """Use the same injection and font workflows as Bokucho, entirely under ``root``."""
    assert patch.recipe is not None
    report = log or (lambda _message: None)
    report("正在解開補丁資料。")
    _unpack_recipe(patch.recipe, root)
    metadata = json.loads((root / "recipe.json").read_text(encoding="utf-8"))
    if metadata.get("engine") in RGSS_SUFFIXES:
        return _compile_rgss(folder, patch, root, metadata, report)
    return _compile_livemaker(folder, patch, root, metadata, report)


def _recipe_file_allowed(name: str, parts: tuple[str, ...]) -> bool:
    suffix = Path(name).suffix.lower()
    return (
        name in {"recipe.json", "proxy_map.json", UIF_DLL, FONT_LICENSE}
        or len(parts) == 2 and parts[0] == "strings" and name.endswith(".json")
        or len(parts) == 2 and parts[0] == "fonts" and suffix in {".ttf", ".otf", ".ttc"}
        or parts[0] == "out" and len(parts) > 1 and suffix in IMAGE_SUFFIXES
    )


def _unpack_recipe(recipe: bytes, root: Path) -> None:
    seen: set[str] = set()
    with ZipFile(io.BytesIO(recipe)) as archive:
        for info in archive.infolist():
            parts = safe_parts(info.filename)
            key = info.filename.lower()
            if key in seen:
                raise ValueError(f"Duplicate recipe file: {info.filename}")
            seen.add(key)
            if not _recipe_file_allowed(info.filename, parts):
                raise ValueError(f"Unexpected recipe file: {info.filename}")
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as out:
                shutil.copyfileobj(source, out)


def _compile_livemaker(
    folder: Path,
    patch: Patch,
    root: Path,
    metadata: dict,
    report: Callable[[str], None],
) -> tuple[dict[str, Path], tuple[PatchAdded, ...]]:
    project = _livemaker_project(folder, patch, root, metadata)

    def adapter(current: Project) -> LiveMakerAdapter:
        return LiveMakerAdapter(root / "proxy_map.json", root / "unpacked")

    report("正在注入翻譯資料。")
    inject_translations(project, adapter(project), root / "out")
    added: tuple[PatchAdded, ...] = ()
    try:
        report("正在重建遊戲字型。")
        result = patch_game_font(project, adapter, out_dir=root / "build")
    except NoPixelFont:
        report("遊戲使用系統字型，正在建立字型載入檔。")
        loader = root / UIF_DLL
        if not loader.is_file():
            raise ValueError(
                "This recipe has no UIF winmm.dll for the game's system font."
            ) from None
        result = patch_system_font(project, adapter, out_dir=root / "build")
        added = (
            PatchAdded(UIF_DLL, loader.read_bytes()),
            PatchAdded(UIF_CONFIG, uif_config(result.font.name, result.family).encode("utf-8")),
            PatchAdded(result.font.name, result.font.read_bytes()),
        )
    if result.archive is None or not result.archive.is_file():
        raise ValueError("Compilation produced no game archive.")
    return {patch.files[0].name: result.archive}, added


def _livemaker_project(folder: Path, patch: Patch, root: Path, metadata: dict) -> Project:
    font = FontConfig.model_validate(metadata["font"])
    for source in font.sources:
        parts = safe_parts(source.path)
        if len(parts) != 2 or parts[0] != "fonts":
            raise ValueError("A font source must be inside the recipe.")
    project = Project(
        root,
        ProjectConfig(
            name=metadata["name"],
            source_lang=metadata["source_lang"],
            target_lang=metadata["target_lang"],
            font=font,
            engine=EngineConfig(
                type="livemaker", game_path=str((folder / patch.files[0].name).resolve())
            ),
        ),
    )
    for strings in project.load_string_files():
        for unit in strings.units:
            if unit.engine_ref.get("lsb"):
                safe_parts(unit.engine_ref["lsb"])
    return project


def _compile_rgss(
    folder: Path,
    patch: Patch,
    root: Path,
    metadata: dict,
    report: Callable[[str], None],
) -> tuple[dict[str, Path], tuple[PatchAdded, ...]]:
    """Inject into the player's own XP, VX or VX Ace data: a rebuilt archive, or loose files."""
    from bokucho.engines import rgssad
    from bokucho.engines.rpgmaker import RpgMakerAdapter

    suffix = RGSS_SUFFIXES[metadata["engine"]]
    archive_name = metadata.get("archive")
    archive = None
    game = folder
    if archive_name is not None:
        if [item.name for item in patch.files] != [archive_name]:
            raise ValueError("A packed RGSS recipe must name its archive alone.")
        archive = folder / archive_name
        game = root / "game"
        report("正在讀取遊戲封包。")
        _unpack_rgss_data(archive, game, suffix)
    project = _rgss_project(root, metadata, game)
    out = root / "out"
    report("正在注入翻譯資料。")
    inject_translations(project, RpgMakerAdapter(), out)
    scripts = _rgss_scripts_name(patch, archive, game, suffix)
    added: tuple[PatchAdded, ...] = ()
    fill = metadata.get("font_fill")
    if fill is not None:
        added = _fill_font(folder, root, fill, out / scripts, game / scripts, report)
    if archive is None and not (out / scripts).is_file() and (game / scripts).is_file():
        if scripts in {item.name for item in patch.files}:
            (out / scripts).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(game / scripts, out / scripts)
    produced = {
        path.relative_to(out).as_posix(): path for path in sorted(out.rglob("*")) if path.is_file()
    }
    if archive is not None:
        report("正在重建遊戲封包。")
        rebuilt = root / "build" / archive_name
        rebuilt.parent.mkdir(parents=True, exist_ok=True)
        rgssad.rebuild(
            archive, {name: path.read_bytes() for name, path in produced.items()}, rebuilt
        )
        return {archive_name: rebuilt}, added
    named = {item.name for item in patch.files}
    if set(produced) != named:
        raise ValueError(
            "Compilation produced other files than the patch names: "
            + ", ".join(sorted(set(produced) ^ named))
        )
    return produced, added


def _unpack_rgss_data(archive: Path, game: Path, suffix: str) -> None:
    from bokucho.engines import rgssad

    for entry in rgssad.entries(archive):
        folder_name, _, name = entry.path.partition("/")
        if folder_name.lower() == "data" and name.lower().endswith(suffix):
            target = game.joinpath("Data", *safe_parts(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rgssad.read(archive, entry))


def _rgss_project(root: Path, metadata: dict, game: Path) -> Project:
    engine = metadata["engine"]
    project = Project(
        root,
        ProjectConfig(
            name=metadata["name"],
            source_lang=metadata["source_lang"],
            target_lang=metadata["target_lang"],
            engine=EngineConfig(type=engine, game_path=str(game.resolve())),
        ),
    )
    for strings in project.load_string_files():
        for unit in strings.units:
            if unit.target and unit.kind != "image":
                if len(safe_parts(str(unit.engine_ref.get("file", "")))) != 1:
                    raise ValueError(f"Invalid {engine} data file: {unit.engine_ref.get('file')}")
    return project


def _rgss_scripts_name(patch: Patch, archive: Path | None, game: Path, suffix: str) -> str:
    """The Scripts file as the patch, or the game's archive, names it: a font fill rewrites it."""
    scripts_name = f"scripts{suffix}"
    if archive is None:
        return next(
            (
                item.name
                for item in patch.files
                if PurePosixPath(item.name).name.lower() == scripts_name
            ),
            f"Data/Scripts{suffix}",
        )
    found = [p for p in (game / "Data").glob("*") if p.name.lower() == scripts_name]
    return f"Data/{found[0].name}" if found else f"Data/Scripts{suffix}"


def _fill_font(
    folder: Path,
    root: Path,
    fill: dict,
    scripts: Path,
    original_scripts: Path,
    report: Callable[[str], None],
) -> tuple[PatchAdded, ...]:
    """The game's font with the recipe's characters added, beside it in the Fonts folder,
    and its license; the loader in ``scripts``. Nothing when the player's game has no such
    font: the translation applies."""
    from fontTools.ttLib import TTFont

    from bokucho.application.proxy_font import merged_font
    from bokucho.engines.rpgmaker_scripts import with_font_loader
    from bokucho.patcher.core import sha256_of

    base_name, face = str(fill.get("base", "")), fill.get("face")
    family, file = str(fill.get("family", "")), str(fill.get("file", ""))
    parts = safe_parts(base_name)
    if type(face) is not int or face < 0:
        raise ValueError(f"Invalid font face: {face!r}")
    if not _FILL_FILE.fullmatch(file) or not _FILL_FAMILY.fullmatch(family):
        raise ValueError(f"Invalid font fill names: {file!r}, {family!r}")
    report("正在補上遊戲字型缺的字。")
    base = folder.joinpath(*parts)
    if not base.is_file():
        report(f"找不到遊戲字型 {base_name}，略過補字；翻譯照常套用。")
        return ()
    if sha256_of(base) != fill.get("sha256"):
        report(f"遊戲字型 {base_name} 和製作補丁時的版本不同，補上的字可能不完整。")
    source = scripts if scripts.is_file() else original_scripts
    try:
        with TTFont(base, fontNumber=face) as game_font, TTFont(root / "fonts" / "fill.ttf") as add:
            merged = root / "build" / file
            merged.parent.mkdir(parents=True, exist_ok=True)
            merged_font(game_font, add, family).save(merged)
        loaded = with_font_loader(source.read_bytes(), family)
    except Exception as error:  # noqa: BLE001 - a font fill never stops the translation
        report(f"無法補上遊戲字型 {base_name} 缺的字（{error}），略過補字；翻譯照常套用。")
        return ()
    scripts.parent.mkdir(parents=True, exist_ok=True)
    scripts.write_bytes(loaded)
    return (
        PatchAdded("/".join((*parts[:-1], file)), merged.read_bytes()),
        PatchAdded("Bokucho-font-license.txt", (root / FONT_LICENSE).read_bytes()),
    )
