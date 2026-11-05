from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from bokucho.application.assets import image_slug
from bokucho.application.errors import InvalidProject
from bokucho.application.image_animation import (
    ANIMATION_MANIFEST_NAME,
    AnimationFrame,
    ImageAnimationProject,
    RasterLayer,
    animation_workspace_path,
    create_image_animation,
    load_image_animation,
    save_image_animation,
    validate_image_animation,
)
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.infrastructure.pillow_image_codec import PillowImageCodec


def _project(tmp_path: Path):
    return create_project(
        CreateProjectRequest(
            tmp_path / "project",
            "Images",
            "ja",
            "zh-Hant",
            "livemaker",
        )
    )


def _png(
    color: tuple[int, int, int, int] = (255, 0, 0, 255),
    size: tuple[int, int] = (5, 4),
) -> bytes:
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


def _animation() -> ImageAnimationProject:
    return ImageAnimationProject(
        relative_path="ui/button.png",
        width=5,
        height=4,
        frames=(
            AnimationFrame(
                id="frame-1",
                duration_ms=100,
                layers=(RasterLayer("layer-1", "Layer 1", _png()),),
            ),
        ),
    )


def test_create_image_animation_bootstraps_existing_flat_image() -> None:
    png = _png()

    animation = create_image_animation(
        "ui\\button.png",
        png,
        PillowImageCodec(),
        source_fingerprint="source-sha256",
    )

    assert animation.relative_path == "ui/button.png"
    assert (animation.width, animation.height) == (5, 4)
    assert animation.frames[0].layers[0].png == png
    assert animation.active_frame == animation.output_frame == 0
    assert animation.source_fingerprint == "source-sha256"


def test_manifest_uses_slug_workspace_and_verified_png_blobs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    animation = _animation()

    result = save_image_animation(project, animation)

    expected_workspace = (
        project.root / ".bokucho" / "image-editor" / image_slug(animation.relative_path)
    )
    assert result.manifest_path == expected_workspace / ANIMATION_MANIFEST_NAME
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    blob = result.blob_paths[0]
    assert manifest["version"] == 1
    assert manifest["frames"][0]["layers"][0]["blob"].startswith(
        "animation/frames/0000/layers/0000-"
    )
    assert (
        manifest["frames"][0]["layers"][0]["sha256"]
        == hashlib.sha256(blob.read_bytes()).hexdigest()
    )


def test_load_rejects_unsupported_manifest_and_tampered_blob(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = save_image_animation(project, _animation())
    original_manifest = result.manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(original_manifest)
    manifest["version"] = 2
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(InvalidProject, match="Unsupported.*version"):
        load_image_animation(project, "ui/button.png", PillowImageCodec())

    result.manifest_path.write_text(original_manifest, encoding="utf-8")
    result.blob_paths[0].write_bytes(_png((0, 255, 0, 255)))
    with pytest.raises(InvalidProject, match="checksum"):
        load_image_animation(project, "ui/button.png", PillowImageCodec())


def test_load_rejects_blob_path_escape(tmp_path: Path) -> None:
    project = _project(tmp_path)
    result = save_image_animation(project, _animation())
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["frames"][0]["layers"][0]["blob"] = "../base.png"
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(InvalidProject, match="Unsafe animation blob path"):
        load_image_animation(project, "ui/button.png", PillowImageCodec())


def test_load_treats_a_changed_game_source_as_a_new_animation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    animation = replace(_animation(), source_fingerprint="old-source")
    save_image_animation(project, animation)

    assert (
        load_image_animation(
            project,
            animation.relative_path,
            PillowImageCodec(),
            source_fingerprint="new-source",
        )
        is None
    )


def test_load_treats_an_external_overlay_change_as_authoritative(tmp_path: Path) -> None:
    project = _project(tmp_path)
    animation = replace(_animation(), output_fingerprint="saved-overlay")
    save_image_animation(project, animation)

    assert (
        load_image_animation(
            project,
            animation.relative_path,
            PillowImageCodec(),
            output_fingerprint="external-overlay",
        )
        is None
    )
    assert (
        load_image_animation(
            project,
            animation.relative_path,
            PillowImageCodec(),
            output_fingerprint="saved-overlay",
        )
        == animation
    )


def test_successful_save_removes_stale_blobs_without_touching_editor_base(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    animation = _animation()
    two_layers = replace(
        animation,
        frames=(
            replace(
                animation.frames[0],
                layers=(
                    animation.frames[0].layers[0],
                    RasterLayer("layer-2", "Layer 2", _png((0, 0, 255, 255))),
                ),
            ),
        ),
    )
    first = save_image_animation(project, two_layers)
    stale_blob = first.blob_paths[1]
    base_path = animation_workspace_path(project, animation.relative_path) / "base.png"
    base_path.write_bytes(b"existing editor base")

    second = save_image_animation(project, animation)

    assert not stale_blob.exists()
    assert second.blob_paths[0].is_file()
    assert base_path.read_bytes() == b"existing editor base"


def test_previous_manifest_remains_loadable_if_process_stops_before_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import bokucho.application.image_animation as persistence

    project = _project(tmp_path)
    animation = _animation()
    first = save_image_animation(project, animation)
    changed = replace(
        animation,
        frames=(
            replace(
                animation.frames[0],
                layers=(
                    replace(
                        animation.frames[0].layers[0],
                        png=_png((0, 255, 0, 255)),
                    ),
                ),
            ),
        ),
    )
    original_write = persistence.atomic_write_bytes

    class SimulatedProcessStop(BaseException):
        pass

    def stop_before_manifest(path: Path, data: bytes) -> None:
        if path.name == ANIMATION_MANIFEST_NAME:
            raise SimulatedProcessStop
        original_write(path, data)

    monkeypatch.setattr(persistence, "atomic_write_bytes", stop_before_manifest)

    with pytest.raises(SimulatedProcessStop):
        save_image_animation(project, changed)

    assert first.blob_paths[0].is_file()
    assert (
        load_image_animation(
            project,
            animation.relative_path,
            PillowImageCodec(),
        )
        == animation
    )


@pytest.mark.parametrize(
    ("animation", "message"),
    [
        (replace(_animation(), frames=()), "at least one frame"),
        (replace(_animation(), active_frame=1), "active frame"),
        (
            replace(
                _animation(),
                frames=(
                    replace(
                        _animation().frames[0],
                        layers=(
                            replace(
                                _animation().frames[0].layers[0],
                                png=_png(size=(2, 2)),
                            ),
                        ),
                    ),
                ),
            ),
            "dimensions",
        ),
        (
            replace(
                _animation(),
                frames=(replace(_animation().frames[0], id="../frame"),),
            ),
            "frame id",
        ),
    ],
)
def test_invalid_animation_state_is_rejected(
    animation: ImageAnimationProject,
    message: str,
) -> None:
    with pytest.raises(InvalidProject, match=message):
        validate_image_animation(animation, PillowImageCodec())
