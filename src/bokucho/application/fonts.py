"""Draw the translation's characters into the game's own font."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject
from bokucho.application.projects import resolve_game_path
from bokucho.engines.cp932_proxy import ProxyMap
from bokucho.engines.livepixelfont import PixelFont, stride
from bokucho.project import Project

try:
    from fontTools.ttLib import TTCollection, TTFont
    from PIL import Image, ImageFont

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _IMPORT_ERROR = exc

_MISSING_DEP_MSG = (
    "Drawing glyphs needs Pillow and fontTools. Install them with:\n"
    "    pip install 'bokucho[images]'"
)

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()

MIN_BOX = 14


@dataclass(frozen=True, slots=True)
class FontPatchResult:
    font: Path
    drawn: int
    widened: int
    undrawable: str
    scripts: int
    slots: int
    archive: Path | None


@dataclass(frozen=True, slots=True)
class _Face:
    path: Path
    codepoints: frozenset[int]
    variation: str | None
    sizes: dict[int, ImageFont.FreeTypeFont] = field(
        default_factory=dict, compare=False, repr=False
    )


def _load_faces(project: Project) -> list[_Face]:
    sources = project.config.font.sources
    if not sources:
        raise InvalidProject(
            "No font source configured. Add a [[font.source]] table naming a "
            ".ttf/.otf to draw the proxied characters from."
        )
    faces = []
    for source in sources:
        path = project.root / source.path
        if not path.is_file():
            raise InvalidProject(f"Font source not found: {path}")
        if path.suffix.lower() == ".ttc":
            face = TTCollection(str(path)).fonts[0]
        else:
            face = TTFont(str(path), fontNumber=0)
        faces.append(_Face(path, frozenset(face.getBestCmap()), source.variation or None))
    return faces


def _draw(ch: str, face: _Face, width: int, height: int) -> bytes:
    """Rasterise ``ch`` as large as fits in ``width`` x ``height``, centred."""
    glyph = None
    for size in range(height + 6, 5, -1):
        font = face.sizes.get(size)
        if font is None:
            font = ImageFont.truetype(str(face.path), size)
            if face.variation:
                font.set_variation_by_name(face.variation)
            face.sizes[size] = font
        mask = font.getmask(ch, mode="L")
        rendered = Image.frombytes("L", mask.size, bytes(mask))
        box = rendered.getbbox()
        if box is None:
            continue
        glyph = rendered.crop(box)
        if glyph.width <= width and glyph.height <= height:
            break
    if glyph is None:
        return bytes(stride(width) * height)
    if glyph.width > width or glyph.height > height:
        glyph = glyph.resize((width, height), Image.LANCZOS)
    canvas = Image.new("L", (stride(width), height), 0)
    canvas.paste(glyph, ((width - glyph.width) // 2, (height - glyph.height) // 2))
    return canvas.tobytes()


def _redraw_donors(
    font: PixelFont, proxy: ProxyMap, faces: list[_Face]
) -> tuple[int, int, str]:
    """Redraw every donor code point as the character it stands in for."""
    boxes = [
        (glyph.width, glyph.height, glyph.bearing_x, glyph.bearing_y)
        for glyph in (font.glyph(donor) for donor in set(proxy.real2proxy.values()))
        if glyph is not None and glyph.width >= MIN_BOX and glyph.height >= MIN_BOX
    ]
    if not boxes:
        raise InvalidProject(
            f"No donor character has a cell big enough to draw in inside {font.name!r}."
        )
    fallback = tuple(int(statistics.median(column)) for column in zip(*boxes, strict=True))

    drawn = widened = 0
    undrawable = []
    for real, donor in sorted(proxy.real2proxy.items()):
        glyph = font.glyph(donor)
        if glyph is None:
            raise InvalidProject(f"Donor {donor!r} has no glyph in {font.name!r}.")
        if glyph.width < MIN_BOX or glyph.height < MIN_BOX:
            glyph.width, glyph.height, glyph.bearing_x, glyph.bearing_y = fallback
            widened += 1
        face = next((f for f in faces if ord(real) in f.codepoints), None)
        if face is None:
            undrawable.append(real)
            face = faces[0]
        glyph.pixels = _draw(real, face, glyph.width, glyph.height)
        drawn += 1
    return drawn, widened, "".join(undrawable)


def patch_game_font(
    project: Project,
    adapter_factory,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    *,
    out_dir: Path | None = None,
) -> FontPatchResult:
    """Redraw the game's pixel font, retarget the scripts, rebuild the archive."""
    if _IMPORT_ERROR is not None:
        raise InvalidProject(_MISSING_DEP_MSG) from _IMPORT_ERROR

    adapter = adapter_factory(project)
    overlay = project.root / "out"
    if not any(overlay.rglob("*.lsb")):
        raise InvalidProject("No overlay to patch. Run `bokucho inject` first.")

    map_path = project.root / project.config.engine.proxy_map
    if not map_path.is_file():
        raise InvalidProject("No proxy map. Run `bokucho patch-runtime` first.")
    proxy = ProxyMap.load(map_path)

    game_path = resolve_game_path(project)
    source_font = adapter.pixel_font(game_path)
    faces = _load_faces(project)
    sink.publish(ProgressEvent("font", 1, 3, "font.source.located", detail=source_font.name))
    token.raise_if_cancelled()

    font = PixelFont(source_font)
    drawn, widened, undrawable = _redraw_donors(font, proxy, faces)
    target = overlay / source_font.name
    target.write_bytes(font.to_bytes())
    sink.publish(ProgressEvent("font", 2, 3, "font.glyphs.drawn", detail=str(drawn)))
    token.raise_if_cancelled()

    scripts, slots = adapter.retarget_fonts(overlay, font.name, sink, token)
    archive = adapter.repack(game_path, overlay, out_dir or project.root / "dist", sink, token)
    sink.publish(ProgressEvent("font", 3, 3, "font.archive.rebuilt", detail=str(archive)))
    return FontPatchResult(target, drawn, widened, undrawable, scripts, slots, archive)
