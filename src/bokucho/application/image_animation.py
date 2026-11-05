"""Versioned image animation state and rollback-safe persistence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from bokucho.application.assets import image_slug
from bokucho.application.contracts import RasterCodec
from bokucho.application.errors import InvalidProject
from bokucho.project import Project
from bokucho.sync.atomic import atomic_write_bytes

ANIMATION_MANIFEST_VERSION = 1
ANIMATION_MANIFEST_NAME = "animation.json"
_BLOB_ROOT = "animation"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_UNSET = object()


@dataclass(frozen=True, slots=True)
class RasterLayer:
    id: str
    name: str
    png: bytes
    visible: bool = True
    opacity: float = 1.0
    locked: bool = False


@dataclass(frozen=True, slots=True)
class AnimationFrame:
    id: str
    duration_ms: int
    layers: tuple[RasterLayer, ...]


@dataclass(frozen=True, slots=True)
class OnionSkinSettings:
    enabled: bool = False
    previous_frames: int = 1
    next_frames: int = 1
    opacity: float = 0.35


@dataclass(frozen=True, slots=True)
class ImageAnimationProject:
    relative_path: str
    width: int
    height: int
    frames: tuple[AnimationFrame, ...]
    active_frame: int = 0
    output_frame: int = 0
    onion_skin: OnionSkinSettings = field(default_factory=OnionSkinSettings)
    source_fingerprint: str | None = None
    output_fingerprint: str | None = None
    painted: bool = False


@dataclass(frozen=True, slots=True)
class ImageAnimationSaveResult:
    manifest_path: Path
    blob_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: Path
    data: bytes | None


def _normalized_image_path(raw: str) -> str:
    normalized = PurePosixPath(raw.replace("\\", "/"))
    if (
        not raw
        or "\x00" in raw
        or normalized.is_absolute()
        or normalized == PurePosixPath(".")
        or ".." in normalized.parts
        or any(":" in part for part in normalized.parts)
    ):
        raise InvalidProject(f"Unsafe image path: {raw}")
    return normalized.as_posix()


def _inside(path: Path, root: Path, message: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise InvalidProject(message) from error
    return path


def animation_workspace_path(project: Project, relative_path: str) -> Path:
    """Return the isolated animation workspace for a safe project-relative image."""
    normalized = _normalized_image_path(relative_path)
    workspace = project.root / ".bokucho" / "image-editor" / image_slug(normalized)
    return _inside(
        workspace,
        project.root,
        "Animation workspace resolves outside the project.",
    )


def create_image_animation(
    relative_path: str,
    base_png: bytes,
    codec: RasterCodec,
    *,
    source_fingerprint: str | None = None,
    output_fingerprint: str | None = None,
) -> ImageAnimationProject:
    """Create the backward-compatible one-frame project used on first edit."""
    raster = _decode_png(codec, base_png, "base layer")
    return ImageAnimationProject(
        relative_path=_normalized_image_path(relative_path),
        width=raster.width,
        height=raster.height,
        frames=(
            AnimationFrame(
                id="frame-1",
                duration_ms=100,
                layers=(RasterLayer("layer-1", "Layer 1", base_png),),
            ),
        ),
        source_fingerprint=source_fingerprint,
        output_fingerprint=output_fingerprint,
    )


def validate_image_animation(
    animation: ImageAnimationProject,
    codec: RasterCodec,
) -> None:
    """Check editor state read from the page or from disk before it is used."""
    if animation.width <= 0 or animation.height <= 0:
        raise InvalidProject("Animation canvas dimensions must be positive integers.")
    if not animation.frames:
        raise InvalidProject("An animation must contain at least one frame.")
    if not 0 <= animation.active_frame < len(animation.frames):
        raise InvalidProject("Animation active frame is outside the timeline.")
    if not 0 <= animation.output_frame < len(animation.frames):
        raise InvalidProject("Animation output frame is outside the timeline.")
    _validate_onion_skin(animation.onion_skin)
    frame_ids: set[str] = set()
    for frame in animation.frames:
        _validate_identifier(frame.id, "frame")
        if frame.id in frame_ids:
            raise InvalidProject(f"Duplicate animation frame id: {frame.id}")
        frame_ids.add(frame.id)
        _validate_frame(frame, animation, codec)


def _validate_frame(
    frame: AnimationFrame, animation: ImageAnimationProject, codec: RasterCodec
) -> None:
    if frame.duration_ms <= 0:
        raise InvalidProject("Animation frame duration must be a positive integer.")
    if not frame.layers:
        raise InvalidProject(f"Animation frame {frame.id} has no raster layers.")
    layer_ids: set[str] = set()
    for layer in frame.layers:
        _validate_identifier(layer.id, "layer")
        if layer.id in layer_ids:
            raise InvalidProject(f"Duplicate raster layer id in frame {frame.id}: {layer.id}")
        layer_ids.add(layer.id)
        if not layer.name.strip():
            raise InvalidProject("Raster layer names cannot be empty.")
        if not 0.0 <= layer.opacity <= 1.0:
            raise InvalidProject("Raster layer opacity must be between 0 and 1.")
        raster = _decode_png(codec, layer.png, f"raster layer {layer.id}")
        if (raster.width, raster.height) != (animation.width, animation.height):
            raise InvalidProject(
                f"Raster layer {layer.id} dimensions do not match the animation canvas."
            )


def save_image_animation(
    project: Project,
    animation: ImageAnimationProject,
) -> ImageAnimationSaveResult:
    """Atomically replace one animation manifest and its managed PNG blobs."""
    workspace = animation_workspace_path(project, animation.relative_path)
    manifest_path = _workspace_file(workspace, ANIMATION_MANIFEST_NAME)
    manifest, blobs = _serialize(animation, workspace)
    planned_paths = {manifest_path, *blobs}
    existing_paths = set(_managed_paths(workspace))
    snapshots = _capture(tuple(sorted(existing_paths | planned_paths, key=str)))

    try:
        for path, data in blobs.items():
            atomic_write_bytes(path, data)
        atomic_write_bytes(manifest_path, manifest)
        for stale in sorted(existing_paths - planned_paths, key=str, reverse=True):
            stale.unlink(missing_ok=True)
        _cleanup_blob_directories(workspace)
    except Exception as error:
        try:
            _restore(snapshots)
            _cleanup_blob_directories(workspace)
        except OSError as rollback_error:
            raise InvalidProject(
                f"Animation save failed and rollback was incomplete: {rollback_error}"
            ) from error
        raise
    return ImageAnimationSaveResult(manifest_path, tuple(blobs))


def load_image_animation(
    project: Project,
    relative_path: str,
    codec: RasterCodec,
    *,
    source_fingerprint: str | None = None,
    output_fingerprint: str | None | object = _UNSET,
) -> ImageAnimationProject | None:
    """Load and verify a persisted animation, or return ``None`` if absent."""
    normalized = _normalized_image_path(relative_path)
    workspace = animation_workspace_path(project, normalized)
    manifest_path = _workspace_file(workspace, ANIMATION_MANIFEST_NAME)
    if not manifest_path.is_file():
        return None
    raw = _read_manifest(manifest_path, normalized)
    if source_fingerprint is not None and raw.get("source_fingerprint") != source_fingerprint:
        return None
    if output_fingerprint is not _UNSET and raw.get("output_fingerprint") != output_fingerprint:
        return None
    canvas = _mapping(raw.get("canvas"), "animation canvas")
    frames_raw = raw.get("frames")
    if not isinstance(frames_raw, list):
        raise InvalidProject("Animation frames must be a JSON array.")
    blob_paths: set[Path] = set()
    animation = ImageAnimationProject(
        relative_path=normalized,
        width=_integer(canvas.get("width"), "animation canvas width"),
        height=_integer(canvas.get("height"), "animation canvas height"),
        frames=tuple(_load_frame(workspace, frame, blob_paths) for frame in frames_raw),
        active_frame=_integer(raw.get("active_frame"), "active frame"),
        output_frame=_integer(raw.get("output_frame"), "output frame"),
        onion_skin=_load_onion_skin(raw.get("onion_skin")),
        source_fingerprint=_optional_text(raw.get("source_fingerprint"), "source fingerprint"),
        output_fingerprint=_optional_text(raw.get("output_fingerprint"), "output fingerprint"),
        painted=_boolean(raw.get("painted", True), "painted"),
    )
    validate_image_animation(animation, codec)
    return animation


def _read_manifest(manifest_path: Path, normalized: str) -> dict[str, object]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidProject(f"Unable to read image animation manifest: {error}") from error
    if not isinstance(raw, dict):
        raise InvalidProject("Image animation manifest must be a JSON object.")
    if raw.get("version") != ANIMATION_MANIFEST_VERSION:
        raise InvalidProject(f"Unsupported image animation manifest version: {raw.get('version')}")
    if raw.get("image") != normalized:
        raise InvalidProject("Image animation manifest belongs to a different asset.")
    return raw


def _load_frame(workspace: Path, value: object, blob_paths: set[Path]) -> AnimationFrame:
    frame = _mapping(value, "animation frame")
    layers = frame.get("layers")
    if not isinstance(layers, list):
        raise InvalidProject("Animation frame layers must be a JSON array.")
    return AnimationFrame(
        id=_text(frame.get("id"), "animation frame id"),
        duration_ms=_integer(frame.get("duration_ms"), "frame duration"),
        layers=tuple(_load_layer(workspace, layer, blob_paths) for layer in layers),
    )


def _load_layer(workspace: Path, value: object, blob_paths: set[Path]) -> RasterLayer:
    layer = _mapping(value, "raster layer")
    blob_path = _blob_path(workspace, layer.get("blob"))
    if blob_path in blob_paths:
        raise InvalidProject("Animation raster layers cannot share a blob path.")
    blob_paths.add(blob_path)
    try:
        png = blob_path.read_bytes()
    except OSError as error:
        raise InvalidProject(
            f"Unable to read animation raster layer {blob_path.name}: {error}"
        ) from error
    if layer.get("sha256") != _sha256(png):
        raise InvalidProject(f"Animation raster layer checksum does not match: {blob_path.name}")
    return RasterLayer(
        id=_text(layer.get("id"), "raster layer id"),
        name=_text(layer.get("name"), "raster layer name"),
        png=png,
        visible=_boolean(layer.get("visible"), "layer visibility"),
        opacity=_number(layer.get("opacity"), "layer opacity"),
        locked=_boolean(layer.get("locked"), "layer lock"),
    )


def _load_onion_skin(value: object) -> OnionSkinSettings:
    onion = _mapping(value, "onion skin settings")
    return OnionSkinSettings(
        enabled=_boolean(onion.get("enabled"), "onion skin enabled"),
        previous_frames=_integer(onion.get("previous_frames"), "previous onion frames"),
        next_frames=_integer(onion.get("next_frames"), "next onion frames"),
        opacity=_number(onion.get("opacity"), "onion skin opacity"),
    )


def _serialize(
    animation: ImageAnimationProject,
    workspace: Path,
) -> tuple[bytes, dict[Path, bytes]]:
    blobs: dict[Path, bytes] = {}
    frames = [
        {
            "id": frame.id,
            "duration_ms": frame.duration_ms,
            "layers": [
                _serialize_layer(workspace, frame_index, layer_index, layer, blobs)
                for layer_index, layer in enumerate(frame.layers)
            ],
        }
        for frame_index, frame in enumerate(animation.frames)
    ]
    manifest = {
        "version": ANIMATION_MANIFEST_VERSION,
        "image": animation.relative_path,
        "source_fingerprint": animation.source_fingerprint,
        "output_fingerprint": animation.output_fingerprint,
        "painted": animation.painted,
        "canvas": {"width": animation.width, "height": animation.height},
        "active_frame": animation.active_frame,
        "output_frame": animation.output_frame,
        "onion_skin": asdict(animation.onion_skin),
        "frames": frames,
    }
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    return encoded + b"\n", blobs


def _serialize_layer(
    workspace: Path,
    frame_index: int,
    layer_index: int,
    layer: RasterLayer,
    blobs: dict[Path, bytes],
) -> dict[str, object]:
    fingerprint = _sha256(layer.png)
    relative_blob = PurePosixPath(
        _BLOB_ROOT,
        "frames",
        f"{frame_index:04d}",
        "layers",
        f"{layer_index:04d}-{fingerprint}.png",
    ).as_posix()
    blobs[_workspace_file(workspace, relative_blob)] = layer.png
    return {
        "id": layer.id,
        "name": layer.name,
        "blob": relative_blob,
        "sha256": fingerprint,
        "visible": layer.visible,
        "opacity": layer.opacity,
        "locked": layer.locked,
    }


def _managed_paths(workspace: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    manifest = _workspace_file(workspace, ANIMATION_MANIFEST_NAME)
    if manifest.is_file():
        paths.append(manifest)
    blob_root = _workspace_file(workspace, _BLOB_ROOT)
    if blob_root.is_dir():
        for path in blob_root.rglob("*"):
            if path.is_file():
                paths.append(_inside(path, workspace, "Unsafe animation blob path."))
    return tuple(paths)


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


def _cleanup_blob_directories(workspace: Path) -> None:
    blob_root = workspace / _BLOB_ROOT
    if not blob_root.is_dir():
        return
    directories = sorted(
        (path for path in blob_root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in (*directories, blob_root):
        try:
            directory.rmdir()
        except OSError:
            pass


def _workspace_file(workspace: Path, relative: str) -> Path:
    path = workspace / Path(*PurePosixPath(relative).parts)
    return _inside(path, workspace, "Animation storage path resolves outside its workspace.")


def _blob_path(workspace: Path, raw: object) -> Path:
    if not isinstance(raw, str) or "\\" in raw:
        raise InvalidProject("Animation blob path must be normalized text.")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != _BLOB_ROOT
        or relative.suffix.lower() != ".png"
        or any(":" in part for part in relative.parts)
    ):
        raise InvalidProject(f"Unsafe animation blob path: {raw}")
    return _workspace_file(workspace, relative.as_posix())


def _decode_png(codec: RasterCodec, data: bytes, label: str):
    try:
        raster = codec.decode(data)
    except (OSError, ValueError) as error:
        raise InvalidProject(f"Unable to decode animation {label}: {error}") from error
    if (raster.format or "").upper() != "PNG":
        raise InvalidProject(f"Animation {label} must use PNG encoding.")
    return raster


def _validate_identifier(value: str, label: str) -> None:
    if _ID_PATTERN.fullmatch(value) is None:
        raise InvalidProject(f"Invalid animation {label} id: {value}")


def _validate_onion_skin(settings: OnionSkinSettings) -> None:
    if settings.previous_frames < 0 or settings.next_frames < 0:
        raise InvalidProject("Onion skin frame counts must be non-negative integers.")
    if not 0.0 <= settings.opacity <= 1.0:
        raise InvalidProject("Onion skin opacity must be between 0 and 1.")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidProject(f"{label.capitalize()} must be a JSON object.")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise InvalidProject(f"Animation {label} must be text.")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise InvalidProject(f"Animation {label} must be text or null.")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidProject(f"Animation {label} must be an integer.")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidProject(f"Animation {label} must be numeric.")
    return float(value)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidProject(f"Animation {label} must be a boolean value.")
    return value
