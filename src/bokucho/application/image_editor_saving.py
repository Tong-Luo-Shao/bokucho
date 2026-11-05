"""Transactional persistence for raster and OCR text-layer edits."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from bokucho import images as images_mod
from bokucho.application.assets import image_slug
from bokucho.application.contracts import RasterCodec
from bokucho.application.errors import InvalidProject
from bokucho.application.image_animation import (
    ANIMATION_MANIFEST_NAME,
    ImageAnimationProject,
    animation_workspace_path,
    load_image_animation,
    save_image_animation,
)
from bokucho.application.image_editing import (
    ImageDocument,
    ImageSaveResult,
    load_image_document,
    save_image_document,
)
from bokucho.application.image_typesetting import ImageTextLayer, save_image_text_layer
from bokucho.project import Project
from bokucho.sync.atomic import atomic_write_bytes, file_lock


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: Path
    data: bytes | None


@dataclass(frozen=True, slots=True)
class _AnimationTreeSnapshot:
    workspace: Path
    files: tuple[tuple[Path, bytes], ...]


def _affected_paths(project: Project, document: ImageDocument) -> tuple[Path, ...]:
    relative = PurePosixPath(document.relative_path.replace("\\", "/"))
    native_relative = Path(*relative.parts)
    slug = image_slug(relative.as_posix())
    paths = (
        project.root / "out" / native_relative,
        project.strings_dir / images_mod.IMAGES_STRINGS_FILE,
        project.root / ".bokucho" / "image-editor" / slug / "base.png",
    )
    return tuple(dict.fromkeys(paths))


def _capture(paths: tuple[Path, ...]) -> tuple[_FileSnapshot, ...]:
    snapshots: list[_FileSnapshot] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = None
        snapshots.append(_FileSnapshot(path, data))
    return tuple(snapshots)


def _restore(snapshots: tuple[_FileSnapshot, ...]) -> None:
    for snapshot in reversed(snapshots):
        if snapshot.data is None:
            snapshot.path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(snapshot.path, snapshot.data)


def _capture_animation_tree(
    project: Project,
    relative_path: str,
) -> _AnimationTreeSnapshot:
    workspace = animation_workspace_path(project, relative_path)
    manifest = workspace / ANIMATION_MANIFEST_NAME
    files: list[tuple[Path, bytes]] = []
    if manifest.is_file():
        files.append((manifest, manifest.read_bytes()))
    blob_root = workspace / "animation"
    if blob_root.is_dir():
        files.extend(
            (path, path.read_bytes())
            for path in sorted(blob_root.rglob("*"), key=str)
            if path.is_file()
        )
    return _AnimationTreeSnapshot(workspace, tuple(files))


def _restore_animation_tree(snapshot: _AnimationTreeSnapshot) -> None:
    manifest = snapshot.workspace / ANIMATION_MANIFEST_NAME
    manifest.unlink(missing_ok=True)
    blob_root = snapshot.workspace / "animation"
    if blob_root.is_dir():
        for path in sorted(blob_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        blob_root.rmdir()
    for path, data in snapshot.files:
        atomic_write_bytes(path, data)


def save_image_editor_document(
    project: Project,
    document: ImageDocument,
    edited_png: bytes,
    base_png: bytes,
    layer: ImageTextLayer,
    codec: RasterCodec,
    *,
    animation: ImageAnimationProject | None = None,
) -> ImageSaveResult:
    """Save raster, text, and animation metadata as one rollback-safe operation."""
    with file_lock(project.strings_dir / images_mod.IMAGES_STRINGS_FILE):
        painted_before = animation is not None and _still_painted(project, document, codec)
        snapshots = _capture(_affected_paths(project, document))
        animation_snapshot = (
            _capture_animation_tree(project, document.relative_path)
            if animation is not None
            else None
        )
        try:
            result = save_image_document(project, document, edited_png, codec)
            if layer.source or layer.target or layer.region is not None or layer.regions:
                save_image_text_layer(
                    project,
                    document.relative_path,
                    layer,
                    base_png=base_png,
                    source_fingerprint=document.source_fingerprint,
                )
            if animation is not None:
                save_image_animation(
                    project,
                    replace(
                        animation,
                        relative_path=document.relative_path,
                        source_fingerprint=document.source_fingerprint,
                        output_fingerprint=result.output_fingerprint,
                        painted=painted_before
                        or not codec.same_picture(edited_png, document.editor_image_bytes),
                    ),
                )
        except Exception as error:
            try:
                _restore(snapshots)
                if animation_snapshot is not None:
                    _restore_animation_tree(animation_snapshot)
            except OSError as rollback_error:
                raise InvalidProject(
                    f"Image save failed and rollback was incomplete: {rollback_error}"
                ) from error
            raise
        return result


def _still_painted(project: Project, document: ImageDocument, codec: RasterCodec) -> bool:
    saved = load_image_animation(
        project,
        document.relative_path,
        codec,
        source_fingerprint=document.source_fingerprint,
        output_fingerprint=document.output_fingerprint,
    )
    return saved is not None and saved.painted


def painted_by_hand(project: Project, relative_path: str, codec: RasterCodec) -> bool:
    """Whether the draft on disk is still a picture painted in the canvas editor."""
    if not (animation_workspace_path(project, relative_path) / ANIMATION_MANIFEST_NAME).is_file():
        return False
    return _still_painted(project, load_image_document(project, relative_path, codec), codec)
