"""Persistent, renderer-neutral image text layer state."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from bokucho import images as images_mod
from bokucho.application.assets import (
    held_image_units,
    image_slug,
    load_image_units,
    save_image_units,
)
from bokucho.application.errors import InvalidProject
from bokucho.model import TextUnit
from bokucho.project import Project
from bokucho.sync.atomic import atomic_write_bytes


@dataclass(frozen=True, slots=True)
class ImageTextStyle:
    font_family: str = ""
    font_size: int = 24
    fill: str = "#FF182230"
    stroke: str = "#FFFFFFFF"
    stroke_width: int = 0
    background: str = "#00000000"
    alignment: str = "center"


@dataclass(frozen=True, slots=True)
class ImageTextRegion:
    """One box OCR read, with what it said."""

    box: tuple[int, int, int, int]
    text: str = ""
    score: float | None = None


@dataclass(frozen=True, slots=True)
class ImageTextLayer:
    source: str = ""
    target: str = ""
    region: tuple[int, int, int, int] | None = None
    style: ImageTextStyle = field(default_factory=ImageTextStyle)
    regions: tuple[ImageTextRegion, ...] = ()


def _workspace_dir(project: Project, relative_path: str) -> Path:
    return project.root / ".bokucho" / "image-editor" / image_slug(relative_path)


def _editor_output_path(project: Project, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path.replace("\\", "/"))
    return project.root / "out" / Path(*relative.parts)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _typesetting(unit: TextUnit) -> dict[str, object] | None:
    value = unit.engine_ref.get("typesetting")
    return value if isinstance(value, dict) else None


def _valid_region(value: object) -> tuple[int, int, int, int] | None:
    if not (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int) for item in value)
    ):
        return None
    x0, y0, x1, y1 = value
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def _regions(unit: TextUnit) -> tuple[ImageTextRegion, ...]:
    raw = unit.engine_ref.get("regions")
    if not isinstance(raw, list):
        return ()
    regions: list[ImageTextRegion] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        box = _valid_region(entry.get("box"))
        if box is None:
            continue
        score = entry.get("score")
        regions.append(
            ImageTextRegion(
                box=box,
                text=str(entry.get("text", "")),
                score=(
                    float(score)
                    if isinstance(score, int | float) and not isinstance(score, bool)
                    else None
                ),
            )
        )
    return tuple(regions)


def _style(value: object) -> ImageTextStyle:
    if not isinstance(value, dict):
        return ImageTextStyle()
    defaults = ImageTextStyle()
    alignment = str(value.get("alignment", defaults.alignment))
    if alignment not in {"left", "center", "right"}:
        alignment = defaults.alignment
    return ImageTextStyle(
        font_family=str(value.get("font_family", defaults.font_family)),
        font_size=max(1, int(value.get("font_size", defaults.font_size))),
        fill=str(value.get("fill", defaults.fill)),
        stroke=str(value.get("stroke", defaults.stroke)),
        stroke_width=max(0, int(value.get("stroke_width", defaults.stroke_width))),
        background=str(value.get("background", defaults.background)),
        alignment=alignment,
    )


def _region_payload(region: ImageTextRegion) -> dict[str, object]:
    payload: dict[str, object] = {"box": list(region.box), "text": region.text}
    if region.score is not None:
        payload["score"] = region.score
    return payload


def _editor_pair_is_valid(
    project: Project,
    relative_path: str,
    raw: dict[str, object],
    source_fingerprint: str | None,
) -> bool:
    if (
        source_fingerprint is not None
        and raw.get("source_fingerprint") != source_fingerprint
    ):
        return False
    base_fingerprint = raw.get("base_fingerprint")
    output_fingerprint = raw.get("output_fingerprint")
    if not isinstance(base_fingerprint, str) or not isinstance(
        output_fingerprint,
        str,
    ):
        return False
    base_path = _workspace_dir(project, relative_path) / "base.png"
    output_path = _editor_output_path(project, relative_path)
    return (
        _sha256_file(base_path) == base_fingerprint
        and _sha256_file(output_path) == output_fingerprint
    )


def load_image_text_layer(
    project: Project,
    relative_path: str,
    *,
    source_fingerprint: str | None = None,
) -> ImageTextLayer:
    unit = load_image_units(project).by_id(images_mod.unit_id(relative_path))
    if unit is None:
        return ImageTextLayer()
    raw = _typesetting(unit)
    regions = _regions(unit)
    if raw is None or not _editor_pair_is_valid(
        project,
        relative_path,
        raw,
        source_fingerprint,
    ):
        return ImageTextLayer(source=unit.source, target=unit.target, regions=regions)
    return ImageTextLayer(
        source=unit.source,
        target=unit.target,
        region=_valid_region(raw.get("region")),
        style=_style(raw.get("style")),
        regions=regions,
    )


def load_editor_base(
    project: Project,
    relative_path: str,
    *,
    source_fingerprint: str,
) -> bytes | None:
    unit = load_image_units(project).by_id(images_mod.unit_id(relative_path))
    raw = _typesetting(unit) if unit is not None else None
    if raw is None or not _editor_pair_is_valid(
        project,
        relative_path,
        raw,
        source_fingerprint,
    ):
        return None
    path = _workspace_dir(project, relative_path) / "base.png"
    try:
        return path.read_bytes()
    except OSError:
        return None


def save_image_text_layer(
    project: Project,
    relative_path: str,
    layer: ImageTextLayer,
    *,
    base_png: bytes,
    source_fingerprint: str,
) -> TextUnit:
    output_path = _editor_output_path(project, relative_path)
    if not output_path.is_file():
        raise InvalidProject(
            "The saved image output is unavailable for text-layer persistence."
        )
    output_fingerprint = _sha256_file(output_path)
    if output_fingerprint is None:
        raise InvalidProject(
            "The saved image output cannot be read for text-layer persistence."
        )
    with held_image_units(project) as units:
        unit = units.by_id(images_mod.unit_id(relative_path))
        if unit is None:
            unit = TextUnit(
                id=images_mod.unit_id(relative_path),
                source=layer.source,
                kind="image",
                engine_ref={"image": relative_path},
            )
            units.units.append(unit)
        unit.target = layer.target
        if not unit.source and layer.source:
            unit.source = layer.source
        engine_ref = {
            **unit.engine_ref,
            "image": relative_path,
            "typesetting": {
                "region": list(layer.region) if layer.region is not None else None,
                "style": asdict(layer.style),
                "source_fingerprint": source_fingerprint,
                "base_fingerprint": _sha256_bytes(base_png),
                "output_fingerprint": output_fingerprint,
            },
        }
        if layer.regions:
            engine_ref["regions"] = [_region_payload(region) for region in layer.regions]
            unit.source = "\n".join(region.text for region in layer.regions)
        else:
            engine_ref.pop("regions", None)
        unit.engine_ref = engine_ref
        atomic_write_bytes(_workspace_dir(project, relative_path) / "base.png", base_png)
        save_image_units(project, units)
    return unit.model_copy(deep=True)
