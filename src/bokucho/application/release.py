"""Proxy map and patch-executable release workflows."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject, JobCancelled, ReleaseBlocked
from bokucho.application.projects import (
    inject_translations,
    injectable_units,
    resolve_game_path,
)
from bokucho.application.proxy_font import (
    UIF_DLL,
    uif_problem,
)
from bokucho.application.quality import QaReport, run_qa
from bokucho.engines import is_rpgmaker
from bokucho.engines.base import EngineAdapter, NoPixelFont
from bokucho.engines.cp932_proxy import ProxyMap, cp932_ok
from bokucho.exe_icon import exe_icon, with_icon
from bokucho.model import TextUnit
from bokucho.patcher.core import RECORD, backup_path, sha256_of
from bokucho.patcher.payload import FileState, Patch, PatchedFile, patch_executable
from bokucho.project import Project

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()
_log = logging.getLogger(__name__)

AdapterFactory = Callable[[Project], EngineAdapter]


@dataclass(frozen=True, slots=True)
class ProxyMapResult:
    total: int
    added: int
    path: Path
    repaired: int = 0


@dataclass(frozen=True, slots=True)
class PatchResult:
    executable: Path
    files: tuple[str, ...]


PATCHER_STUB = "bokucho-patcher.exe"
PATCHER_BUILD = "uv run --extra build python tools/build.py --patcher-only"
UIF_BUILD = "uv run --extra build python tools/build.py --uif-only"
_LANGUAGE_TAG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class PatcherReleaseResult:
    artifact_dir: Path
    executable: Path
    qa: QaReport
    proxy: ProxyMapResult
    translated: int
    total: int
    files: int


def _safe_remove_stage(stage: Path, parent: Path, prefix: str) -> None:
    if not stage.exists():
        return
    resolved_parent = parent.resolve()
    resolved_stage = stage.resolve()
    if resolved_stage.parent != resolved_parent or not stage.name.startswith(prefix):
        raise RuntimeError(f"Refusing to remove unsafe staging path: {stage}")
    shutil.rmtree(stage)


def _safe_project_name(project: Project) -> str:
    name = project.config.name
    if not name or name in {".", ".."} or Path(name).name != name:
        raise InvalidProject(f"Project name is not safe for an artifact path: {name!r}")
    return name


def patcher_name(project: Project) -> str:
    """What every release is called: ``<project>-<source>2<target>-patcher.exe``."""
    name = _safe_project_name(project)
    source, target = project.config.source_lang, project.config.target_lang
    if not (_LANGUAGE_TAG.fullmatch(source) and _LANGUAGE_TAG.fullmatch(target)):
        raise InvalidProject(
            f"Language tags are not safe for an artifact name: {source!r}, {target!r}"
        )
    return f"{name}-{source}2{target}-patcher.exe"


def _title(project: Project) -> str:
    return patcher_name(project).removesuffix(".exe")


def build_proxy_map(project: Project) -> ProxyMapResult:
    """Build or extend the stable CP932 donor-character map."""
    needed: set[str] = set()
    reserved: set[str] = set()
    units = [unit for string_file in project.load_string_files() for unit in string_file.units]
    if not units:
        raise InvalidProject("No text units found. Run `bokucho extract` first.")
    for unit in units:
        reserved.update(unit.source)
        reserved.update(unit.target)
        if unit.kind != "image":
            needed.update(character for character in unit.target if not cp932_ok(character))
    path = project.root / project.config.engine.proxy_map
    proxy = ProxyMap.load(path) if path.is_file() else ProxyMap()
    repaired = len(proxy.shadowed) + proxy.free_donors(reserved)
    added = proxy.extend(needed, reserved)
    proxy.save(path)
    return ProxyMapResult(len(proxy.real2proxy), added, path, repaired)


def _adapter(adapter_factory: AdapterFactory | None, project: Project) -> EngineAdapter:
    if adapter_factory is None:
        raise ValueError(f"A {project.config.engine.type} patch needs the engine's adapter.")
    return adapter_factory(project)


def _zipped(folder: Path) -> bytes:
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(path for path in folder.rglob("*") if path.is_file()):
            archive.write(path, path.relative_to(folder).as_posix())
    return stream.getvalue()


def _carried(patch: Patch) -> tuple[str, ...]:
    payload = patch.mount if patch.mount is not None else patch.recipe
    if payload is None:
        return ()
    with ZipFile(io.BytesIO(payload)) as archive:
        return tuple(sorted(archive.namelist()))


def _livemaker_inputs(
    project: Project, adapter_factory: AdapterFactory | None, uif: Path | None
) -> tuple[Path, Path | None]:
    """The packed game file and, for a game drawn with a system face, UIF's loader."""
    if not project.config.font.sources:
        raise InvalidProject("Choose the game text font on the release screen first.")
    game_path = resolve_game_path(project)
    if not game_path.is_file():
        raise ReleaseBlocked(f"A patch needs the packed game file, not a folder: {game_path}")
    check_original(project, game_path, record=False)
    loader = None
    pixel_font = getattr(_adapter(adapter_factory, project), "pixel_font", None)
    if callable(pixel_font):
        try:
            pixel_font(game_path)
        except NoPixelFont:
            loader = _uif_dll(project, uif)
    return game_path, loader


def _livemaker_patch(
    project: Project,
    game_path: Path,
    loader: Path | None,
    work: Path,
    token: CancellationToken,
) -> tuple[Patch, bytes]:
    """Translation inputs; the player's patcher injects, draws and repacks."""
    recipe = _recipe_payload(project, loader)
    digest = check_original(project, game_path, record=False)
    original = game_path.read_bytes()
    patch = Patch(
        _title(project),
        (PatchedFile(game_path.name, _file_state(original), None, b"", 0),),
        recipe=recipe,
    )
    _rehearse(patch, game_path.parent, work, token)
    token.raise_if_cancelled()
    _record_original(project, digest)
    return patch, original


def _rehearse(patch: Patch, folder: Path, work: Path, token: CancellationToken) -> None:
    """Compile the recipe against the original as the player's patcher will."""
    from bokucho.patcher.recipe import compile_recipe

    rehearsal = work / "rehearsal"
    rehearsal.mkdir()

    def report(message: str) -> None:
        token.raise_if_cancelled()
        _log.info("Rehearsing the player's patcher: %s", message)

    try:
        compile_recipe(folder, patch, rehearsal, report)
    except JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever stops it stops every player
        raise ReleaseBlocked(
            f"The player's patcher would refuse this patch: {exc}"
        ) from exc
    finally:
        shutil.rmtree(rehearsal, ignore_errors=True)


def _rpgmaker_mount_patch(project: Project, work: Path) -> Patch:
    from bokucho.application.mount_patch import write_rpgmaker_mount

    stage = work / "mount"
    stage.mkdir()
    write_rpgmaker_mount(project, stage)
    return Patch(_title(project), (), mount=_zipped(stage))


def _renpy_mount_patch(project: Project, work: Path) -> Patch:
    from bokucho.application.mount_patch import write_renpy_mount

    stage = work / "mount"
    stage.mkdir()
    write_renpy_mount(project, stage)
    return Patch(_title(project), (), mount=_zipped(stage))


def _overlay_pictures(pictures: Path) -> list[str]:
    """The translated pictures in the project overlay, as paths from the game root."""
    from bokucho.patcher.recipe import IMAGE_SUFFIXES, safe_parts

    images = []
    if not pictures.is_dir():
        return images
    for path in sorted(pictures.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            if not path.resolve().is_relative_to(pictures.resolve()):
                raise ReleaseBlocked(f"Image escapes the project overlay: {path}")
            relative = path.relative_to(pictures).as_posix()
            safe_parts(relative)
            images.append(relative)
    return images


def _planned_font_fill(units: Sequence[TextUnit], layout, scripts: bytes | None):
    from bokucho.application.rgss_font import plan_font_fill

    try:
        return plan_font_fill((unit.target for unit in units if unit.target), layout.root, scripts)
    except Exception as error:  # noqa: BLE001 - a font fill never stops a release
        _log.warning("Font fill skipped: %s", error)
        return None


def _refuse_a_patched_folder(
    units: Sequence[TextUnit], layout, names: Sequence[str]
) -> None:
    """A release is made against the original files, never against a patched game."""
    from bokucho.engines.rpgmaker import DATA_SUFFIX

    #: Before injecting: a patched Scripts literal no longer reads as its source.
    data = layout.relative_data.as_posix()
    touched = {
        f"{data}/{unit.engine_ref.get('file')}{DATA_SUFFIX[layout.variant]}"
        for unit in units
        if unit.target
    }
    candidates = sorted({*touched, *names})
    patched = [name for name in candidates if backup_path(layout.root / name).is_file()]
    if (layout.root / RECORD).is_file():
        patched.append(RECORD)
    if patched:
        raise ReleaseBlocked(
            "The game folder holds a patch already (" + ", ".join(patched[:3]) + "). "
            "Restore the original with the patch first: a release is made against "
            "the original files."
        )


def _unchanged(name: str, size: int, digest: str) -> PatchedFile:
    return PatchedFile(name, FileState(size, digest), None, b"", 0)


def _rgss_files(
    project: Project, layout, names: Sequence[str], images: Sequence[str]
) -> tuple[PatchedFile, ...]:
    """The original files the recipe is checked against before it is applied."""
    from bokucho.engines import rgssad

    if layout.archive is not None:
        held = {entry.path.casefold() for entry in rgssad.entries(layout.archive)}
        stray = [name for name in images if name.casefold() not in held]
        if stray:
            raise ReleaseBlocked(
                f"{layout.archive.name} holds no picture named " + ", ".join(stray)
            )
        digest = check_original(project, layout.archive)
        return (_unchanged(layout.archive.name, layout.archive.stat().st_size, digest),)
    stray = [name for name in names if not (layout.root / name).is_file()]
    if stray:
        raise ReleaseBlocked("The game holds no file named " + ", ".join(stray))
    return tuple(
        _unchanged(name, (layout.root / name).stat().st_size, sha256_of(layout.root / name))
        for name in names
    )


def _rgss_recipe(
    project: Project,
    layout,
    units: Sequence[TextUnit],
    fill,
    pictures: Path,
    images: Sequence[str],
) -> bytes:
    from bokucho.engines.rpgmaker import VARIANT_ENGINES

    #: For the window title in Game.ini. XP's System has no game title: XP's window keeps its own.
    title = next(
        (unit.target for unit in units if unit.id == "System:game_title" and unit.target),
        None,
    )
    metadata = {
        "engine": VARIANT_ENGINES[layout.variant],
        "name": project.config.name,
        "source_lang": project.config.source_lang,
        "target_lang": project.config.target_lang,
        "archive": layout.archive.name if layout.archive is not None else None,
        "title": title,
        "font_fill": None
        if fill is None
        else {
            "base": fill.base,
            "face": fill.face,
            "sha256": fill.sha256,
            "family": fill.family,
            "file": fill.file,
        },
    }
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("recipe.json", json.dumps(metadata, ensure_ascii=False))
        if fill is not None:
            archive.writestr("fonts/fill.ttf", fill.fill)
            archive.writestr("font-license.txt", fill.license)
        for path in sorted(project.strings_dir.glob("*.json")):
            archive.write(path, f"strings/{path.name}")
        for relative in images:
            archive.write(pictures / relative, f"out/{relative}")
    return stream.getvalue()


def _rgss_patch(project: Project, adapter_factory: AdapterFactory | None, work: Path) -> Patch:
    """A recipe the player's patcher injects into the player's own XP, VX or VX Ace data."""
    from bokucho.engines.rpgmaker import DATA_SUFFIX, sniff

    layout = sniff(resolve_game_path(project))
    if layout.archive is not None:
        check_original(project, layout.archive, record=False)
    pictures = project.root / "out"
    images = _overlay_pictures(pictures)
    units = injectable_units(project)
    scripts_name, scripts = _rgss_scripts(layout, f"Scripts{DATA_SUFFIX[layout.variant]}")
    fill = _planned_font_fill(units, layout, scripts)
    #: A loose game's Scripts file carries the font loader, translated or not.
    loader = [scripts_name] if fill is not None and layout.archive is None else []
    if layout.archive is None:
        _refuse_a_patched_folder(units, layout, [*images, *loader])
    overlay = work / "overlay"
    inject_translations(project, _adapter(adapter_factory, project), overlay)
    written = [
        path.relative_to(overlay).as_posix()
        for path in sorted(overlay.rglob("*"))
        if path.is_file()
    ]
    files = _rgss_files(project, layout, sorted({*written, *images, *loader}), images)
    recipe = _rgss_recipe(project, layout, units, fill, pictures, images)
    return Patch(_title(project), files, recipe=recipe)


def _rgss_scripts(layout, name: str) -> tuple[str, bytes | None]:
    """The Scripts file's path from the game root as the game names it, and its bytes."""
    from bokucho.engines import rgssad

    data = layout.relative_data.as_posix()
    wanted = f"{data}/{name}".casefold()
    if layout.archive is not None:
        entry = next(
            (e for e in rgssad.entries(layout.archive) if e.path.casefold() == wanted), None
        )
        return f"{data}/{name}", None if entry is None else rgssad.read(layout.archive, entry)
    found = next(
        (
            path
            for path in sorted(layout.data_dir.glob("*"))
            if f"{data}/{path.name}".casefold() == wanted and path.is_file()
        ),
        None,
    )
    if found is None:
        return f"{data}/{name}", None
    return f"{data}/{found.name}", found.read_bytes()


def _patch_contents(
    project: Project,
    adapter_factory: AdapterFactory | None,
    work: Path,
    livemaker: tuple[Path, Path | None] | None,
    token: CancellationToken = _NEVER_CANCELLED,
) -> tuple[Patch, bytes | None]:
    """The engine's patch, and the game executable whose icon it shows, if any."""
    engine = project.config.engine.type
    if engine == "livemaker":
        assert livemaker is not None
        check_values = getattr(_adapter(adapter_factory, project), "check_values", None)
        if callable(check_values):
            try:
                check_values(injectable_units(project), livemaker[0])
            except ValueError as exc:
                raise ReleaseBlocked(str(exc)) from exc
        return _livemaker_patch(project, *livemaker, work, token)
    if engine == "renpy":
        return _renpy_mount_patch(project, work), _game_program(project)
    if is_rpgmaker(engine):
        from bokucho.engines.rpgmaker import RGSS_VARIANTS, sniff

        if sniff(resolve_game_path(project)).variant in RGSS_VARIANTS:
            return _rgss_patch(project, adapter_factory, work), _game_program(project)
        return _rpgmaker_mount_patch(project, work), _game_program(project)
    raise InvalidProject(f"Bokucho has no patch for the {engine} engine.")


def _game_program(project: Project) -> bytes | None:
    """The game's own executable, whose icon its patch shows; None when none is found."""
    path = resolve_game_path(project)
    if path.is_file() and path.suffix.lower() == ".exe":
        return path.read_bytes()
    folder = path if path.is_dir() else path.parent
    programs = sorted(folder.glob("*.exe"))
    # Ren'Py's launcher is named after its <name>.py; RPG Maker's is Game.exe.
    chosen = next((p for p in programs if p.with_suffix(".py").is_file()), None) or next(
        (p for p in programs if p.name.lower() == "game.exe"), None
    )
    if chosen is None and len(programs) == 1:
        chosen = programs[0]
    return chosen.read_bytes() if chosen is not None else None


def _publish(
    project: Project,
    adapter_factory: AdapterFactory | None,
    destination: Path,
    stub: Path,
    livemaker: tuple[Path, Path | None] | None,
    token: CancellationToken = _NEVER_CANCELLED,
) -> PatchResult:
    """Write the patch executable into ``destination``, and nothing else there."""
    name = patcher_name(project)
    destination.mkdir(parents=True, exist_ok=True)
    work = destination / f".bokucho-patch-{uuid4().hex}"
    work.mkdir()
    try:
        patch, icon_source = _patch_contents(project, adapter_factory, work, livemaker, token)
        program = stub.read_bytes()
        if icon_source is not None and (icon := exe_icon(icon_source)) is not None:
            program = with_icon(program, icon)
        executable = work / name
        executable.write_bytes(patch_executable(program, patch))
        final = destination / name
        os.replace(executable, final)
        return PatchResult(final, _carried(patch))
    finally:
        _safe_remove_stage(work, destination, ".bokucho-patch-")


def build_patch(
    project: Project,
    out_dir: Path | None = None,
    *,
    adapter_factory: AdapterFactory | None = None,
    stub: Path | None = None,
    uif: Path | None = None,
) -> PatchResult:
    """Publish the patch executable into ``out_dir`` (the project's ``dist/``)."""
    patcher_name(project)
    if not any(project.strings_dir.glob("*.json")):
        raise InvalidProject("No text units found. Run `bokucho extract` first.")
    livemaker = None
    if project.config.engine.type == "livemaker":
        livemaker = _livemaker_inputs(project, adapter_factory, uif)
        build_proxy_map(project)
    destination = (out_dir or (project.root / "dist")).resolve()
    return _publish(project, adapter_factory, destination, stub or patcher_stub(), livemaker)


def _next_artifact_path(artifacts: Path) -> Path:
    stem = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = artifacts / stem
    sequence = 2
    while candidate.exists():
        candidate = artifacts / f"{stem}-{sequence}"
        sequence += 1
    return candidate


def patcher_stub(
    candidates: Sequence[Path] | None = None, source: Path | None = None
) -> Path:
    """The patcher program every patch executable starts as.

    The frozen Bokucho carries it; from source, tools/build.py builds it, and
    it is refused once ``source`` -- Bokucho's own code -- changed after: the
    player's game is compiled with the engine the patcher carries.
    """
    if candidates is None:
        bundled = getattr(sys, "_MEIPASS", None)
        candidates = [
            *([Path(bundled) / "bokucho" / "patcher" / PATCHER_STUB] if bundled else []),
            Path(__file__).resolve().parents[3] / "build" / "patcher" / PATCHER_STUB,
        ]
        source = None if bundled else Path(__file__).resolve().parents[1]
    for candidate in candidates:
        if candidate.is_file():
            built = candidate.stat().st_mtime
            newer = (
                next((p for p in source.rglob("*.py") if p.stat().st_mtime > built), None)
                if source is not None
                else None
            )
            if newer is not None:
                raise ReleaseBlocked(
                    f"The patcher program is older than {newer.relative_to(source).as_posix()}. "
                    f"Rebuild it with `{PATCHER_BUILD}`."
                )
            return candidate
    raise ReleaseBlocked(f"The patcher program is missing. Build it with `{PATCHER_BUILD}`.")


def bundled_uif(candidates: Sequence[Path] | None = None) -> Path | None:
    """The winmm.dll Bokucho builds from UIF's source, if this Bokucho has it."""
    if candidates is None:
        bundled = getattr(sys, "_MEIPASS", None)
        candidates = [
            *([Path(bundled) / "bokucho" / "uif" / UIF_DLL] if bundled else []),
            Path(__file__).resolve().parents[3] / "build" / "uif" / UIF_DLL,
        ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def check_original(project: Project, game_path: Path, *, record: bool = True) -> str:
    """The game file's SHA-256: recorded at the first release, and held to after it."""
    digest = sha256_of(game_path)
    recorded = project.config.engine.original_sha256
    if recorded is None:
        if record:
            _record_original(project, digest)
    elif recorded != digest:
        raise ReleaseBlocked(
            f"{game_path.name} is no longer the original the first release was made "
            "against. If the patch was applied to it, restore the original with the "
            "patch first."
        )
    return digest


def _record_original(project: Project, digest: str) -> None:
    if project.config.engine.original_sha256 is None:
        engine = project.config.engine.model_copy(update={"original_sha256": digest})
        project.save_config(project.config.model_copy(update={"engine": engine}))


def _file_state(data: bytes) -> FileState:
    return FileState(len(data), hashlib.sha256(data).hexdigest())


def _uif_dll(project: Project, bundled: Path | None) -> Path:
    """UIF's DLL for a game that draws with a system face: the translator's, else Bokucho's."""
    raw = project.config.engine.uif_dll
    if not raw:
        if bundled is not None:
            return bundled
        raise ReleaseBlocked(
            "This game draws its text with a system face and this Bokucho carries no UIF "
            f"{UIF_DLL} to load the proxy font: build it with `{UIF_BUILD}`, or choose "
            "one on the release screen."
        )
    path = Path(raw) if Path(raw).is_absolute() else project.root / raw
    if path.name.lower() != UIF_DLL:
        raise ReleaseBlocked(
            f"UIF's DLL is {path.name}, which LiveMaker does not load: choose UIF's {UIF_DLL} "
            "on the release screen."
        )
    if not path.is_file():
        raise ReleaseBlocked(f"UIF's DLL is not where project.toml says: {path}")
    if problem := uif_problem(path.read_bytes()):
        raise ReleaseBlocked(
            f"{path} is {problem}: choose UIF's {UIF_DLL} on the release screen."
        )
    return path


def _recipe_payload(project: Project, loader: Path | None) -> bytes:
    from bokucho.patcher.recipe import IMAGE_SUFFIXES, safe_parts

    sources = []
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        for index, source in enumerate(project.config.font.sources):
            path = project.root / source.path
            if not path.is_file():
                raise InvalidProject(f"Font source not found: {path}")
            if path.suffix.lower() not in {".ttf", ".otf", ".ttc"}:
                raise InvalidProject(f"Unsupported font source: {path}")
            relative = f"fonts/{index}{path.suffix.lower()}"
            archive.write(path, relative)
            sources.append({"path": relative, "variation": source.variation})
        metadata = {
            "name": project.config.name,
            "source_lang": project.config.source_lang,
            "target_lang": project.config.target_lang,
            "font": {"sources": sources},
        }
        archive.writestr("recipe.json", json.dumps(metadata, ensure_ascii=False))
        for path in sorted(project.strings_dir.glob("*.json")):
            archive.write(path, f"strings/{path.name}")
        archive.write(project.root / project.config.engine.proxy_map, "proxy_map.json")
        if loader is not None:
            archive.write(loader, UIF_DLL)
        overlay = project.root / "out"
        for path in sorted(overlay.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                if not path.resolve().is_relative_to(overlay.resolve()):
                    raise ReleaseBlocked(f"Image escapes the project overlay: {path}")
                relative = path.relative_to(overlay).as_posix()
                safe_parts(relative)
                archive.write(path, f"out/{relative}")
    return stream.getvalue()


def release_patcher(
    project: Project,
    adapter_factory: AdapterFactory,
    artifacts: Path,
    stub: Path,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    *,
    uif: Path | None = None,
) -> PatcherReleaseResult:
    """Publish ``artifacts/<time>/`` holding the patch executable alone, whatever the engine."""
    patcher_name(project)
    livemaker = None
    if project.config.engine.type == "livemaker":
        livemaker = _livemaker_inputs(project, adapter_factory, uif)
    token.raise_if_cancelled()
    proxy = build_proxy_map(project)
    sink.publish(ProgressEvent("release", 1, 3, "release.proxy.completed"))
    token.raise_if_cancelled()
    qa = run_qa(project)
    sink.publish(ProgressEvent("release", 2, 3, "release.qa.completed", detail=str(len(qa.issues))))
    token.raise_if_cancelled()
    output_root = artifacts.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    stage = output_root / f".bokucho-release-{uuid4().hex}"
    stage.mkdir()
    try:
        published = _publish(project, adapter_factory, stage, stub, livemaker, token)
        sink.publish(ProgressEvent("release", 3, 3, "release.patcher.completed"))
        token.raise_if_cancelled()
        artifact_dir = _next_artifact_path(output_root)
        stage.rename(artifact_dir)
        units = injectable_units(project)
        return PatcherReleaseResult(
            artifact_dir,
            artifact_dir / published.executable.name,
            qa,
            proxy,
            sum(bool(unit.target) for unit in units),
            len(units),
            len({unit.engine_ref.get("lsb") or unit.engine_ref.get("file") for unit in units}),
        )
    finally:
        _safe_remove_stage(stage, output_root, ".bokucho-release-")
