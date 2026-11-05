"""Source-preserving image document loading and persistence."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from bokucho import images as images_mod
from bokucho.application.contracts import RasterCodec, RasterImage
from bokucho.application.errors import InvalidProject
from bokucho.application.image_typesetting import load_editor_base
from bokucho.application.projects import resolve_game_root
from bokucho.project import Project
from bokucho.sync.atomic import atomic_write_bytes

EDITABLE_IMAGE_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA"})


@dataclass(frozen=True, slots=True)
class ImageDocument:
    relative_path: str
    source_path: Path
    image_bytes: bytes
    editor_image_bytes: bytes
    width: int
    height: int
    mode: str
    format: str | None
    has_alpha: bool
    source_fingerprint: str
    output_fingerprint: str | None
    encoding_reference: RasterImage

    @property
    def is_editable(self) -> bool:
        if self.mode not in EDITABLE_IMAGE_MODES:
            return False
        return self.mode != "P" or self.encoding_reference.palette is not None


@dataclass(frozen=True, slots=True)
class ImageSaveResult:
    output_path: Path
    output_fingerprint: str


def _safe_relative_path(raw: str) -> Path:
    normalized = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or normalized.is_absolute()
        or normalized == PurePosixPath(".")
        or ".." in normalized.parts
        or (normalized.parts and ":" in normalized.parts[0])
    ):
        raise InvalidProject(f"Unsafe image path: {raw}")
    return Path(*normalized.parts)


def _source_path(project: Project, relative_path: str) -> tuple[Path, Path]:
    relative = _safe_relative_path(relative_path)
    game_root = resolve_game_root(project).resolve()
    source = (game_root / relative).resolve()
    try:
        source.relative_to(game_root)
    except ValueError as error:
        raise InvalidProject(f"Unsafe image path: {relative_path}") from error
    if not source.is_file():
        raise InvalidProject(f"Image does not exist: {relative.as_posix()}")
    return relative, source


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint_data(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ensure_source_is_preserved(
    output: Path,
    source: Path,
    project_root: Path,
) -> None:
    resolved_output = output.resolve()
    try:
        resolved_output.relative_to(project_root.resolve())
    except ValueError as error:
        raise InvalidProject("Image edit output resolves outside the project.") from error
    if resolved_output == source.resolve() or (output.exists() and output.samefile(source)):
        raise InvalidProject("Image edit output resolves to the original game source.")


def _working_output_path(project: Project, relative: Path) -> Path:
    return project.root / "out" / relative


def _output_fingerprint(path: Path | None, codec: RasterCodec, source: Path) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        return _fingerprint_data(codec.load(path, source=source).data)
    except (OSError, ValueError) as error:
        raise InvalidProject(f"Unable to verify saved image edit {path}: {error}") from error


def load_image_document(
    project: Project,
    relative_path: str,
    codec: RasterCodec,
) -> ImageDocument:
    """Load an editable raster snapshot without changing the game source."""
    relative, source = _source_path(project, relative_path)
    frames = images_mod.frame_count(source)
    if frames > 1:
        raise InvalidProject(
            f"{relative.as_posix()} holds {frames} animation frames. The editor "
            "saves a single frame, which would drop the rest."
        )
    source_fingerprint = _fingerprint(source)
    try:
        raster = codec.load(source)
    except (OSError, ValueError) as error:
        raise InvalidProject(f"Unable to load image {relative.as_posix()}: {error}") from error
    saved = _saved_edit(_working_output_path(project, relative), source, raster, codec)
    working = raster if saved is None else saved
    mode = working.mode if raster.mode == "P" and working.palette is None else raster.mode
    base = _editor_base(project, relative.as_posix(), source_fingerprint, raster, codec)
    return ImageDocument(
        relative.as_posix(),
        source,
        working.data,
        working.data if base is None else base,
        raster.width,
        raster.height,
        mode,
        raster.format,
        raster.has_alpha,
        source_fingerprint,
        None if saved is None else _fingerprint_data(saved.data),
        replace(working, icc_profile=raster.icc_profile, exif=raster.exif),
    )


def _saved_edit(working_path: Path, source: Path, raster, codec: RasterCodec):
    """The edit saved to the overlay, which must be the source's size; None if none is."""
    if not working_path.is_file():
        return None
    try:
        working = codec.load(working_path, source=source)
    except (OSError, ValueError) as error:
        raise InvalidProject(f"Unable to load image edit {working_path}: {error}") from error
    if (working.width, working.height) != (raster.width, raster.height):
        raise InvalidProject(
            "Saved image edit dimensions do not match the game source: "
            f"{working.width} x {working.height} != {raster.width} x {raster.height}."
        )
    return working


def _editor_base(
    project: Project, relative: str, source_fingerprint: str, raster, codec: RasterCodec
) -> bytes | None:
    """The picture the editor last painted over, if it is still readable and the right size."""
    saved_base = load_editor_base(project, relative, source_fingerprint=source_fingerprint)
    if saved_base is None:
        return None
    try:
        base_raster = codec.decode(saved_base)
    except (OSError, ValueError):
        return None
    if (base_raster.width, base_raster.height) != (raster.width, raster.height):
        return None
    return base_raster.data


def save_image_document(
    project: Project,
    document: ImageDocument,
    edited_bytes: bytes,
    codec: RasterCodec,
) -> ImageSaveResult:
    """Persist an edited raster to the overlay under the asset's own name."""
    relative, source = _source_path(project, document.relative_path)
    if source != document.source_path.resolve():
        raise InvalidProject("Image document no longer matches the configured game source.")
    if _fingerprint(source) != document.source_fingerprint:
        raise InvalidProject("The game source changed since the editor was opened.")
    if _output_fingerprint(_working_output_path(project, relative), codec, source) != (
        document.output_fingerprint
    ):
        raise InvalidProject("The saved image edit changed since the editor was opened.")
    if not document.is_editable:
        raise InvalidProject(
            f"Image mode {document.mode} cannot be edited without loss in this editor."
        )
    try:
        edited = codec.decode(edited_bytes)
    except (OSError, ValueError) as error:
        raise InvalidProject(f"Unable to decode edited image: {error}") from error
    if (edited.width, edited.height) != (document.width, document.height):
        raise InvalidProject(
            "Edited image dimensions must remain "
            f"{document.width} x {document.height}; got {edited.width} x {edited.height}."
        )

    try:
        encoded = codec.encode(
            edited,
            relative.suffix.lower(),
            document.mode,
            reference=document.encoding_reference,
            source=source,
        )
    except (OSError, ValueError) as error:
        raise InvalidProject(f"Unable to encode edited image: {error}") from error

    output = project.root / "out" / relative
    _ensure_source_is_preserved(output, source, project.root)
    output_fingerprint = _fingerprint_data(
        codec.decode(encoded, source=source).data
    )
    atomic_write_bytes(output, encoded)
    return ImageSaveResult(output, output_fingerprint)
