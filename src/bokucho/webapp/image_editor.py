"""JSON adapters for the layered web image editor."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from typing import Any

from bokucho.application.contracts import RasterCodec
from bokucho.application.errors import ValidationFailure
from bokucho.application.image_animation import (
    AnimationFrame,
    ImageAnimationProject,
    OnionSkinSettings,
    RasterLayer,
    create_image_animation,
    load_image_animation,
    validate_image_animation,
)
from bokucho.application.image_editing import ImageDocument, load_image_document
from bokucho.application.image_editor_saving import save_image_editor_document
from bokucho.application.image_typesetting import (
    ImageTextLayer,
    ImageTextRegion,
    ImageTextStyle,
    load_image_text_layer,
)
from bokucho.project import Project


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _decode(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValidationFailure(f"{label} must be Base64 text.")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValidationFailure(f"{label} is not valid Base64 data.") from error


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationFailure(f"{label} must be an object.")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationFailure(f"{label} must be text.")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationFailure(f"{label} must be an integer.")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationFailure(f"{label} must be numeric.")
    return float(value)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationFailure(f"{label} must be a boolean value.")
    return value


def _serialize_animation(animation: ImageAnimationProject) -> dict[str, Any]:
    return {
        "active_frame": animation.active_frame,
        "output_frame": animation.output_frame,
        "onion_skin": asdict(animation.onion_skin),
        "frames": [
            {
                "id": frame.id,
                "duration_ms": frame.duration_ms,
                "layers": [
                    {
                        "id": layer.id,
                        "name": layer.name,
                        "data": _encode(layer.png),
                        "visible": layer.visible,
                        "opacity": layer.opacity,
                        "locked": layer.locked,
                    }
                    for layer in frame.layers
                ],
            }
            for frame in animation.frames
        ],
    }


def _serialize_region(region: ImageTextRegion) -> dict[str, Any]:
    payload: dict[str, Any] = {"box": list(region.box), "text": region.text}
    if region.score is not None:
        payload["score"] = region.score
    return payload


def _serialize_text_layer(layer: ImageTextLayer) -> dict[str, Any]:
    return {
        "source": layer.source,
        "target": layer.target,
        "region": list(layer.region) if layer.region is not None else None,
        "style": asdict(layer.style),
        "regions": [_serialize_region(region) for region in layer.regions],
    }


def _serialize_palette(document: ImageDocument) -> list[str]:
    palette = document.encoding_reference.palette
    if palette is None:
        return []
    return [f"#{red:02x}{green:02x}{blue:02x}{alpha:02x}" for red, green, blue, alpha in palette]


def load_editor_state(
    project: Project,
    relative_path: str,
    codec: RasterCodec,
) -> dict[str, Any]:
    """Load one browser-ready layered editor document."""
    document = load_image_document(project, relative_path, codec)
    animation = load_image_animation(
        project,
        document.relative_path,
        codec,
        source_fingerprint=document.source_fingerprint,
        output_fingerprint=document.output_fingerprint,
    )
    if animation is None:
        animation = create_image_animation(
            document.relative_path,
            document.editor_image_bytes,
            codec,
            source_fingerprint=document.source_fingerprint,
            output_fingerprint=document.output_fingerprint,
        )
    text_layer = load_image_text_layer(
        project,
        document.relative_path,
        source_fingerprint=document.source_fingerprint,
    )
    return {
        "path": document.relative_path,
        "width": document.width,
        "height": document.height,
        "mode": document.mode,
        "format": document.format,
        "has_alpha": document.has_alpha,
        "editable": document.is_editable,
        "source_fingerprint": document.source_fingerprint,
        "output_fingerprint": document.output_fingerprint,
        "palette": _serialize_palette(document),
        "text_layer": _serialize_text_layer(text_layer),
        **_serialize_animation(animation),
    }


def _box(value: object, label: str) -> tuple[int, int, int, int]:
    if not (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        raise ValidationFailure(f"{label} must hold four integers.")
    x0, y0, x1, y1 = value
    if x0 < 0 or y0 < 0 or x0 >= x1 or y0 >= y1:
        raise ValidationFailure(f"{label} is invalid.")
    return x0, y0, x1, y1


def _parse_regions(value: object) -> tuple[ImageTextRegion, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValidationFailure("Text layer regions must be an array.")
    regions: list[ImageTextRegion] = []
    for index, entry in enumerate(value):
        label = f"BBox {index + 1}"
        raw = _mapping(entry, label)
        score = raw.get("score")
        regions.append(
            ImageTextRegion(
                box=_box(raw.get("box"), label),
                text=_text(raw.get("text", ""), f"{label} text"),
                score=None if score is None else _number(score, f"{label} score"),
            )
        )
    return tuple(regions)


def _parse_text_layer(value: object) -> ImageTextLayer:
    raw = _mapping(value, "Text layer")
    style_raw = _mapping(raw.get("style", {}), "Text layer style")
    style_fields = {
        key: style_raw[key] for key in ImageTextStyle.__dataclass_fields__ if key in style_raw
    }
    region = raw.get("region")
    return ImageTextLayer(
        source=_text(raw.get("source", ""), "Text layer source"),
        target=_text(raw.get("target", ""), "Text layer target"),
        region=None if region is None else _box(region, "Text layer region"),
        style=ImageTextStyle(**style_fields),
        regions=_parse_regions(raw.get("regions")),
    )


def _parse_layer(value: object, label: str) -> RasterLayer:
    raw = _mapping(value, label)
    return RasterLayer(
        id=_text(raw.get("id"), "Layer id"),
        name=_text(raw.get("name"), "Layer name"),
        png=_decode(raw.get("data"), "Layer image"),
        visible=_boolean(raw.get("visible"), "Layer visibility"),
        opacity=_number(raw.get("opacity"), "Layer opacity"),
        locked=_boolean(raw.get("locked"), "Layer lock"),
    )


def _parse_frame(value: object, label: str) -> AnimationFrame:
    raw = _mapping(value, label)
    layers = raw.get("layers")
    if not isinstance(layers, list):
        raise ValidationFailure(f"{label} layers must be an array.")
    return AnimationFrame(
        id=_text(raw.get("id"), "Frame id"),
        duration_ms=_integer(raw.get("duration_ms"), "Frame duration"),
        layers=tuple(_parse_layer(layer, f"{label} layer") for layer in layers),
    )


def _parse_animation(
    document: ImageDocument,
    value: object,
    codec: RasterCodec,
) -> ImageAnimationProject:
    raw = _mapping(value, "Editor state")
    frames = raw.get("frames")
    if not isinstance(frames, list):
        raise ValidationFailure("Editor frames must be an array.")
    onion_raw = _mapping(raw.get("onion_skin"), "Onion skin settings")
    animation = ImageAnimationProject(
        relative_path=document.relative_path,
        width=document.width,
        height=document.height,
        frames=tuple(
            _parse_frame(frame, f"Frame {index + 1}") for index, frame in enumerate(frames)
        ),
        active_frame=_integer(raw.get("active_frame"), "Active frame"),
        output_frame=_integer(raw.get("output_frame"), "Output frame"),
        onion_skin=OnionSkinSettings(
            enabled=_boolean(onion_raw.get("enabled"), "Onion skin enabled"),
            previous_frames=_integer(
                onion_raw.get("previous_frames"),
                "Previous onion frames",
            ),
            next_frames=_integer(onion_raw.get("next_frames"), "Next onion frames"),
            opacity=_number(onion_raw.get("opacity"), "Onion skin opacity"),
        ),
        source_fingerprint=document.source_fingerprint,
        output_fingerprint=document.output_fingerprint,
    )
    validate_image_animation(animation, codec)
    return animation


def save_editor_state(
    project: Project,
    relative_path: str,
    source_fingerprint: str,
    state: object,
    codec: RasterCodec,
) -> dict[str, Any]:
    """Persist raster, text, layer, and frame state as one transaction."""
    document = load_image_document(project, relative_path, codec)
    if document.source_fingerprint != source_fingerprint:
        raise ValidationFailure("The game source changed since the editor was opened.")
    raw = _mapping(state, "Editor state")
    animation = _parse_animation(document, raw, codec)
    text_layer = _parse_text_layer(raw.get("text_layer", {}))
    output_png = _decode(raw.get("output_data"), "Output frame")
    base_png = _decode(raw.get("base_data", raw.get("output_data")), "Editor base")
    result = save_image_editor_document(
        project,
        document,
        output_png,
        base_png,
        text_layer,
        codec,
        animation=animation,
    )
    return {
        "output_path": str(result.output_path),
        "output_fingerprint": result.output_fingerprint,
    }
