"""Automatic erase + typeset for game pictures (Phase 7b)."""

from __future__ import annotations

import hashlib
import io
import os
import unicodedata
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    from bokucho.engines.rpgmv_assets import ENCRYPTED_SUFFIXES, encrypt, key_for, open_bytes
    from bokucho.gal import save_gal
except ModuleNotFoundError:
    from gal import save_gal
    from rpgmv_assets import ENCRYPTED_SUFFIXES, encrypt, key_for, open_bytes

EXTREME_PERCENTILE = 0.5
GLYPH_CUT = 0.5
OUTLINE_SHARE = 0.10
OUTLINE_COVER = 0.10
DILATE_PX = 2
SEED_INSET_PX = 2
SURROUND_PX = 4
FLAT_PLATE_SPREAD = 6.0
MIN_SIZE_RATIO = 0.6
BOX_PAD_RATIO = 0.25
BOX_PAD_PX = 4
MODEL_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
MODEL_SHA256 = "344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9"
MODEL_BYTES = 205_669_692
MODEL_NAME = "big-lama.pt"
FONT_PROBE = (
    r"C:\Windows\Fonts\msjhbd.ttc",
    r"C:\Windows\Fonts\msjh.ttc",
    r"C:\Windows\Fonts\kaiu.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
GUI_LAYOUT_STYLE_KEYS = frozenset({"font_family", "font_size", "alignment", "background"})


def panel_luminance(lum: np.ndarray, region: tuple[int, int, int, int]) -> float:
    """What the text sits on: the median of the box and a ring around it."""
    x0, y0, x1, y1 = region
    pad = 3
    ring = lum[max(y0 - pad, 0):min(y1 + pad, lum.shape[0]),
               max(x0 - pad, 0):min(x1 + pad, lum.shape[1])]
    return float(np.median(ring))


def _reach(
    lum: np.ndarray,
    region: tuple[int, int, int, int],
    panel: float,
    aside: np.ndarray | None = None,
) -> tuple[float, float]:
    """How far the box's lightest and darkest pixels get from the panel."""
    inner = lum[region[1]:region[3], region[0]:region[2]]
    if aside is not None:
        inner = inner[~aside[region[1]:region[3], region[0]:region[2]]]
        if inner.size == 0:
            return 0.0, 0.0
    return (
        float(np.percentile(inner, 100 - EXTREME_PERCENTILE)) - panel,
        panel - float(np.percentile(inner, EXTREME_PERCENTILE)),
    )


def detect_polarity(lum: np.ndarray, region: tuple[int, int, int, int]) -> str:
    """Which side of the panel the text is on: lighter than it, or darker."""
    panel = panel_luminance(lum, region)
    above, below = _reach(lum, region, panel)
    return "light-on-dark" if above > below else "dark-on-light"


def build_glyph_mask(
    lum: np.ndarray,
    region: tuple[int, int, int, int],
    polarity: str,
    aside: np.ndarray | None = None,
) -> np.ndarray:
    """Boolean full-image mask of text pixels inside ``region`` (undilated)."""
    x0, y0, x1, y1 = region
    panel = panel_luminance(lum, region)
    above, below = _reach(lum, region, panel, aside)
    mask = np.zeros(lum.shape, dtype=bool)
    inner = lum[y0:y1, x0:x1]
    mask[y0:y1, x0:x1] = (
        inner < panel - below * GLYPH_CUT
        if polarity == "dark-on-light"
        else inner > panel + above * GLYPH_CUT
    )
    if aside is not None:
        mask &= ~aside
    return mask


def off_the_plate(mask: np.ndarray, region: tuple[int, int, int, int]) -> np.ndarray:
    """The parts of ``mask`` that run across ``region``, from one edge to the other."""
    x0, y0, x1, y1 = region
    window = mask[y0:y1, x0:x1]

    def reaching(edge: tuple[slice | int, slice | int]) -> np.ndarray:
        seeds = np.zeros(window.shape, dtype=bool)
        seeds[edge] = True
        return _joined(window, seeds)

    across = (reaching(np.s_[:, 0]) & reaching(np.s_[:, -1])) | (
        reaching(np.s_[0, :]) & reaching(np.s_[-1, :])
    )
    reached = np.zeros(mask.shape, dtype=bool)
    reached[y0:y1, x0:x1] = across
    return reached


def dilate(mask: np.ndarray, px: int = DILATE_PX) -> np.ndarray:
    """Grow a boolean mask by ``px`` pixels (square kernel)."""
    img = Image.fromarray(mask.astype(np.uint8) * 255)
    return np.array(img.filter(ImageFilter.MaxFilter(2 * px + 1))) > 0


def surrounds(mask: np.ndarray, px: int = SURROUND_PX) -> np.ndarray:
    """The band of picture just outside ``mask``."""
    return dilate(mask, px) & ~mask


def _seeded(low: int, high: int, inset: int, origin: int) -> tuple[int, int]:
    """One side of the box to seed from, inset and put in the window's frame."""
    if high - low > 2 * inset:
        low, high = low + inset, high - inset
    return max(low - origin, 0), max(high - origin, 0)


def connected_to(mask: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    """The parts of ``mask`` that reach inside ``box``, with the rest dropped."""
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return mask
    top, left = int(ys.min()), int(xs.min())
    window = mask[top : int(ys.max()) + 1, left : int(xs.max()) + 1]
    y0, y1 = _seeded(box[1], box[3], SEED_INSET_PX, top)
    x0, x1 = _seeded(box[0], box[2], SEED_INSET_PX, left)
    found = np.zeros(window.shape, dtype=bool)
    found[y0:y1, x0:x1] = window[y0:y1, x0:x1]
    while True:
        grown = dilate(found, 1) & window
        if grown.sum() == found.sum():
            break
        found = grown
    reached = np.zeros(mask.shape, dtype=bool)
    reached[top : top + window.shape[0], left : left + window.shape[1]] = found
    return reached


class Lettering(NamedTuple):
    kept: np.ndarray
    rejected: np.ndarray
    frame: np.ndarray


def lettering_in(
    lum: np.ndarray,
    box: tuple[int, int, int, int],
    core: tuple[int, int, int, int],
    polarity: str,
    others: Sequence[tuple[int, int, int, int]] = (),
) -> Lettering:
    """The lettering a read box holds, and what was found round it that is not."""
    found = _lettering_in(lum, box, core, polarity)
    letters = found.kept & ~off_the_plate(found.kept, box)
    theirs = np.zeros(lum.shape, dtype=bool)
    for other in others:
        theirs[other[1] : other[3], other[0] : other[2]] = True
    letters &= ~_joined(letters, letters & theirs)
    x0, y0, x1, y1 = box
    height, width = lum.shape
    letter = core[3] - core[1]
    wider = (
        max(x0 - letter, 0) if letters[:, x0].any() else x0,
        max(y0 - letter, 0) if letters[y0, :].any() else y0,
        min(x1 + letter, width) if letters[:, x1 - 1].any() else x1,
        min(y1 + letter, height) if letters[y1 - 1, :].any() else y1,
    )
    if wider == box:
        return found
    return _lettering_in(lum, wider, core, polarity)


def _lettering_in(
    lum: np.ndarray,
    box: tuple[int, int, int, int],
    core: tuple[int, int, int, int],
    polarity: str,
) -> Lettering:
    glyph = build_glyph_mask(lum, box, polarity)
    quarter = (core[3] - core[1]) // 4
    middle = np.zeros(lum.shape, dtype=bool)
    middle[core[1] + quarter : core[3] - quarter, core[0] + quarter : core[2] - quarter] = True
    frame = np.zeros(lum.shape, dtype=bool)
    while (edge := off_the_plate(glyph, box)).any() and (
        4 * (glyph & edge & middle).sum() < max((glyph & middle).sum(), 1)
    ):
        frame |= edge
        glyph = build_glyph_mask(lum, box, polarity, frame)
    kept = connected_to(glyph, core)
    return Lettering(kept, (glyph & ~kept) | frame, frame)


def _joined(mask: np.ndarray, seeds: np.ndarray) -> np.ndarray:
    """The parts of ``mask`` joined to ``seeds``."""
    found = seeds & mask
    while True:
        grown = dilate(found, 1) & mask
        if grown.sum() == found.sum():
            return found
        found = grown


def fill_with_surface(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Fill the hole with the one surface around it, which is its own answer."""
    band = surrounds(mask)
    if not band.any():
        return rgb
    out = rgb.copy()
    out[mask] = np.median(rgb[band], axis=0).round().astype(np.uint8)
    return out


def is_flat(lum: np.ndarray, mask: np.ndarray) -> bool:
    """Whether what surrounds the hole is one surface, so it is the answer."""
    band = surrounds(mask)
    if not band.any():
        return False
    ring = lum[band].astype(np.float32)
    return float(np.median(np.abs(ring - np.median(ring)))) <= FLAT_PLATE_SPREAD


@dataclass
class Ink:
    bbox: tuple[int, int, int, int]
    fill: tuple[int, int, int]
    stroke: tuple[int, int, int] | None


def measure_ink(
    rgb: np.ndarray,
    lum: np.ndarray,
    glyph: np.ndarray,
    panel: float,
    reach: float | None = None,
) -> Ink:
    """Measure the original text BEFORE erasing: tight bbox + fill/stroke colors."""
    ys, xs = np.nonzero(glyph)
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    fill = tuple(int(v) for v in rgb[glyph].mean(axis=0))
    lettering = float(lum[glyph].mean())
    if reach is None:
        reach = abs(lettering - panel)
    rim = dilate(glyph) & ~glyph
    away = 1 if lettering >= panel else -1
    beyond = (round(panel) - lum.astype(np.int16)) * away
    outline = rim & (beyond > max(1.0, reach * OUTLINE_SHARE))
    stroke = (
        tuple(int(v) for v in rgb[outline].mean(axis=0))
        if outline.any() and outline.sum() >= rim.sum() * OUTLINE_COVER
        else None
    )
    return Ink(bbox=bbox, fill=fill, stroke=stroke)


def has_glyphs(font_path: str, text: str) -> bool:
    """True if the font's cmap covers every non-whitespace character."""
    from fontTools.ttLib import TTFont

    cmap = TTFont(font_path, fontNumber=0).getBestCmap()
    return all(ord(ch) in cmap for ch in text if not ch.isspace())


@dataclass
class FitResult:
    font: ImageFont.FreeTypeFont
    size: int
    bbox: tuple[int, int, int, int]
    needs_manual: bool
    height_size: int


def _drawn(
    text: str, font: ImageFont.FreeTypeFont
) -> tuple[int, int, int, int, np.ndarray]:
    """The face's own box for ``text``, and the pixels it covers half or more of."""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text, font=font)
    canvas = Image.new("L", (max(right - left, 1), max(bottom - top, 1)))
    ImageDraw.Draw(canvas).text((-left, -top), text, font=font, fill=255)
    return left, top, right, bottom, np.asarray(canvas) >= 128


def drawn_box(text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int, int, int]:
    """Where ``text`` drawn at the origin covers half a pixel or more."""
    left, top, right, bottom, inked = _drawn(text, font)
    ys, xs = np.nonzero(inked)
    if ys.size == 0:
        return left, top, right, bottom
    return (
        left + int(xs.min()),
        top + int(ys.min()),
        left + int(xs.max()) + 1,
        top + int(ys.max()) + 1,
    )


WEIGHT_TOLERANCE = 0.4
OTHER_WEIGHT = {
    "msjhbd.ttc": "msjh.ttc",
    "msjh.ttc": "msjhbd.ttc",
    "NotoSansCJK-Regular.ttc": "NotoSansCJK-Bold.ttc",
    "NotoSansCJK-Bold.ttc": "NotoSansCJK-Regular.ttc",
}


def matching_weight(
    font_path: str, original: str, lettering: np.ndarray, target_h: int
) -> str:
    """``font_path``, or its other weight where the lettering is plainly that one's."""
    other = OTHER_WEIGHT.get(Path(font_path).name)
    sibling = Path(font_path).with_name(other) if other else None
    if (
        font_path not in FONT_PROBE
        or sibling is None
        or not sibling.is_file()
        or not original.strip()
    ):
        return font_path
    ink = int(lettering.sum())

    def distance(face: str) -> float:
        if not has_glyphs(face, original):
            return float("inf")
        size = fit_font(original, face, target_h, 1 << 16).size
        return abs(int(_drawn(original, ImageFont.truetype(face, size, index=0))[4].sum()) - ink)

    mine = distance(font_path)
    if mine <= ink * WEIGHT_TOLERANCE or distance(str(sibling)) >= mine:
        return font_path
    return str(sibling)


def fit_font(
    text: str,
    font_path: str,
    target_h: int,
    max_w: int,
    stroke_width: int = 0,
    like: str | None = None,
    size: int | None = None,
) -> FitResult:
    """Fit ``text`` to the original ink height, shrinking only if too wide."""
    probe = ImageDraw.Draw(Image.new("L", (8, 8)))

    def metrics(size: int):
        f = ImageFont.truetype(font_path, size, index=0)
        return f, probe.textbbox((0, 0), text, font=f, stroke_width=stroke_width)

    def height(size: int) -> int:
        box = drawn_box(like or text, ImageFont.truetype(font_path, size, index=0))
        return box[3] - box[1]

    if size is None:
        size = max(int(target_h), 8)
        while size > 8 and height(size) > target_h:
            size -= 1
        for _ in range(20):
            if height(size + 1) > target_h:
                break
            size += 1
    font, bbox = metrics(size)
    height_size = size
    while size > 1 and bbox[2] - bbox[0] > max_w:
        size -= 1
        font, bbox = metrics(size)
    return FitResult(
        font=font, size=size, bbox=drawn_box(text, font),
        needs_manual=size < height_size * MIN_SIZE_RATIO,
        height_size=height_size,
    )


class ModelMissingError(RuntimeError):
    """The LaMa checkpoint is not on disk (we never download automatically)."""


def model_path() -> Path:
    """Where the checkpoint lives, downloaded or not."""
    env = os.environ.get("BOKUCHO_LAMA_MODEL")
    return Path(env) if env else Path.home() / ".cache" / "bokucho" / MODEL_NAME


def resolve_model_path() -> Path:
    path = model_path()
    if not path.is_file():
        raise ModelMissingError(
            f"LaMa model not found: {path}\n"
            f"Download it once with:\n"
            f'    curl -L --create-dirs -o "{path}" {MODEL_URL}'
        )
    return path


def download_model(progress: Callable[[int, int], None] | None = None) -> Path:
    """Fetch the pinned checkpoint if it is not already there, and return it."""
    destination = model_path()
    if destination.is_file():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    digest = hashlib.sha256()
    completed = 0
    if progress is not None:
        progress(0, MODEL_BYTES)
    with urllib.request.urlopen(MODEL_URL) as response, partial.open("wb") as handle:
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            handle.write(chunk)
            digest.update(chunk)
            completed += len(chunk)
            if progress is not None:
                progress(min(completed, MODEL_BYTES), MODEL_BYTES)
    if digest.hexdigest() != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise ModelMissingError(
            f"The downloaded eraser is not the pinned checkpoint: {digest.hexdigest()}"
        )
    partial.replace(destination)
    return destination


def resolve_font(configured: str | None, root: Path) -> str | None:
    """The font to typeset with: the project's own, else one the machine has."""
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = root / path
        return str(path) if path.is_file() else None
    return next((font for font in FONT_PROBE if Path(font).is_file()), None)


def make_inpaint_fn(model_path: Path):
    """Load the TorchScript LaMa once; return fn(rgb, mask) -> inpainted rgb."""
    try:
        import torch
        import torch.nn.functional as F
    except ImportError as exc:  # pragma: no cover - exercised only without torch
        raise ImportError(
            "Inpainting requires torch. Install it with:\n    pip install 'bokucho[images]'"
        ) from exc

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = torch.jit.load(str(model_path), map_location=device).eval()

    def fn(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        h, w = mask.shape
        img_t = torch.from_numpy(rgb).permute(2, 0, 1).float().unsqueeze(0) / 255.0
        mask_t = torch.from_numpy(mask).float().unsqueeze(0).unsqueeze(0)
        ph, pw = (8 - h % 8) % 8, (8 - w % 8) % 8
        img_t = F.pad(img_t, (0, pw, 0, ph), mode="reflect").to(device)
        mask_t = F.pad(mask_t, (0, pw, 0, ph), mode="reflect").to(device)
        with torch.inference_mode():
            result = model(img_t, mask_t)[0]
        if result.max() <= 1.5:
            result = result * 255.0
        res = result.permute(1, 2, 0).clamp(0, 255).byte().cpu().numpy()[:h, :w]
        composed = rgb.copy()
        composed[mask] = res[mask]
        return composed

    return fn


@dataclass
class RenderResult:
    image: str
    status: str
    reason: str = ""
    size: int | None = None


def _uses_gui_layout(style: dict[str, object]) -> bool:
    return bool(GUI_LAYOUT_STYLE_KEYS.intersection(style))


def _style_font_path(style: dict[str, object], fallback: str) -> str:
    """The face a text layer names, or the project's when it names no font file."""
    family = style.get("font_family")
    if not isinstance(family, str) or not family.strip():
        return fallback
    direct = Path(family)
    return str(direct) if direct.is_file() else fallback


def _rgba(
    value: object,
    fallback: tuple[int, ...] | None,
) -> tuple[int, int, int, int] | None:
    if isinstance(value, str):
        color = value.removeprefix("#")
        try:
            if len(color) == 8:
                alpha = int(color[:2], 16)
                red, green, blue = (
                    int(color[index : index + 2], 16) for index in (2, 4, 6)
                )
                return red, green, blue, alpha
            if len(color) == 6:
                red, green, blue = (
                    int(color[index : index + 2], 16) for index in (0, 2, 4)
                )
                return red, green, blue, 255
        except ValueError:
            pass
    if isinstance(value, (list, tuple)) and len(value) in {3, 4}:
        try:
            channels = tuple(max(0, min(255, int(channel))) for channel in value)
        except (TypeError, ValueError):
            channels = ()
        if len(channels) == 3:
            return channels[0], channels[1], channels[2], 255
        if len(channels) == 4:
            return channels[0], channels[1], channels[2], channels[3]
    if fallback is None:
        return None
    if len(fallback) == 3:
        return fallback[0], fallback[1], fallback[2], 255
    return fallback[0], fallback[1], fallback[2], fallback[3]


def _wrapped_lines(
    text: str,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = character
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


def _draw_gui_text(
    image: Image.Image,
    text: str,
    region: tuple[int, int, int, int],
    style: dict[str, object],
    font_path: str,
    fill: tuple[int, int, int, int],
    stroke: tuple[int, int, int, int] | None,
    stroke_width: int,
) -> bool:
    x0, y0, x1, y1 = region
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    background = _rgba(style.get("background"), None)
    if background is not None and background[3] > 0:
        ImageDraw.Draw(layer).rectangle((0, 0, width, height), fill=background)
    font_size = max(1, int(style.get("font_size", 24)))
    font = ImageFont.truetype(font_path, font_size, index=0)
    text_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)
    padding = max(2, stroke_width + 2)
    available_width = max(1, width - 2 * padding)
    lines = _wrapped_lines(text, draw, font, available_width)
    ascent, descent = font.getmetrics()
    line_height = max(1, ascent + descent)
    total_height = line_height * len(lines)
    top = max(padding, (height - total_height) // 2)
    alignment = style.get("alignment", "center")
    overflow = total_height > max(1, height - 2 * padding)
    for index, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        text_width = bbox[2] - bbox[0]
        left = _line_left(alignment, width, padding, text_width)
        overflow = overflow or text_width > available_width
        draw.text(
            (left - bbox[0], top + index * line_height - bbox[1]),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke,
        )
    layer.alpha_composite(text_layer)
    image.alpha_composite(layer, (x0, y0))
    return overflow


def _line_left(alignment: object, width: int, padding: int, text_width: int) -> float:
    if alignment == "left":
        return padding
    if alignment == "right":
        return width - padding - text_width
    return (width - text_width) / 2


def room(
    lum: np.ndarray,
    panel: float,
    lettering: np.ndarray,
    frame: np.ndarray,
    ink: tuple[int, int, int, int],
    box: tuple[int, int, int, int],
) -> int:
    """How wide a line centred on ``ink`` can be."""
    x0, y0, x1, y1 = ink
    rows = slice(y0, y1)
    walls = frame[rows].any(axis=0)
    if walls[box[0] : box[2]].any():
        free = ~walls
    else:
        flat = np.abs(lum[rows].astype(np.int16) - round(panel)).max(axis=0)
        free = (flat <= FLAT_PLATE_SPREAD) | lettering[rows].any(axis=0)
        free[box[0] : box[2]] = True
        free &= ~walls
    centre = (x0 + x1) / 2
    blocked = np.nonzero(~free)[0]
    before, after = blocked[blocked < centre], blocked[blocked >= centre]
    low = int(before.max()) + 1 if before.size else 0
    high = int(after.min()) if after.size else lum.shape[1]
    reach = max(int(2 * min(centre - low, high - centre)) - 2, 1)
    return reach if walls.any() else max(reach, box[2] - box[0])


def padded_box(
    box: tuple[int, int, int, int],
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    """``box`` grown by a fraction of its own height, clipped to the picture."""
    x0, y0, x1, y1 = box
    margin = max(BOX_PAD_PX, round((y1 - y0) * BOX_PAD_RATIO))
    return (
        max(0, x0 - margin),
        max(0, y0 - margin),
        min(width, x1 + margin),
        min(height, y1 + margin),
    )


def _order_boxes(order: dict[str, object]) -> list[tuple[int, int, int, int]]:
    """Every box the order names, one per line OCR read, else the single region."""
    regions = order.get("regions")
    boxes: list[tuple[int, int, int, int]] = []
    if isinstance(regions, list):
        for region in regions:
            box = region.get("box") if isinstance(region, dict) else None
            if isinstance(box, (list, tuple)) and len(box) == 4:
                boxes.append(tuple(int(value) for value in box))
    if boxes:
        return boxes
    region = order.get("region")
    if isinstance(region, (list, tuple)) and len(region) == 4:
        return [tuple(int(value) for value in region)]
    return []


def _order_sources(order: dict[str, object], count: int) -> list[str]:
    """What each box originally said, so a line that did not change is known."""
    regions = order.get("regions")
    if isinstance(regions, list) and len(regions) == count:
        return [
            str(region.get("text", "")) if isinstance(region, dict) else ""
            for region in regions
        ]
    return [str(order.get("source", ""))] * count


def _run_on(first: str, second: str) -> str:
    """Two parts of one line as the line: a space only between narrow letters."""
    if not first or not second:
        return first + second
    wide = {"W", "F"}
    if {unicodedata.east_asian_width(first[-1]), unicodedata.east_asian_width(second[0])} & wide:
        return first + second
    return f"{first} {second}"


def one_line_each(
    boxes: list[tuple[int, int, int, int]], lines: list[str], sources: list[str]
) -> tuple[list[tuple[int, int, int, int]], list[str], list[str]]:
    """Boxes read off one line of lettering, put back together as that line."""
    merged = [(boxes[0], lines[0], sources[0])]
    for box, line, source in zip(boxes[1:], lines[1:], sources[1:], strict=True):
        last, last_line, last_source = merged[-1]
        letter = min(box[3] - box[1], last[3] - last[1])
        same_row = min(box[3], last[3]) - max(box[1], last[1]) >= letter / 2
        gap = max(box[0], last[0]) - min(box[2], last[2])
        if same_row and gap < letter / 2:
            merged[-1] = (
                (
                    min(box[0], last[0]),
                    min(box[1], last[1]),
                    max(box[2], last[2]),
                    max(box[3], last[3]),
                ),
                _run_on(last_line, line),
                _run_on(last_source, source),
            )
        else:
            merged.append((box, line, source))
    return (
        [box for box, _, _ in merged],
        [line for _, line, _ in merged],
        [source for _, _, source in merged],
    )


def _respaced(text: str) -> str:
    """Spacing is not a change: retyping the same words is not a decision."""
    return "".join(text.split())


def _lines_to_draw(
    order: dict, target: str, gui_layout: bool
) -> tuple[list[tuple[int, int, int, int]], list[str], list[str]] | RenderResult:
    """The boxes whose text changes, with their new and original lines."""
    image = str(order["image"])
    boxes = _order_boxes(order)
    if not boxes:
        return RenderResult(image, "skipped", "no region")
    if not target:
        return RenderResult(image, "skipped", "no target")
    if len(boxes) > 1:
        lines = target.splitlines()
        if len(lines) != len(boxes):
            return RenderResult(image, "needs_manual", "target lines do not match the boxes")
    elif "\n" in target and not gui_layout:
        return RenderResult(image, "needs_manual", "multiline target")
    else:
        lines = [target]
    sources = _order_sources(order, len(boxes))
    if len(boxes) > 1:
        boxes, lines, sources = one_line_each(boxes, lines, sources)
    changed = [
        (box, line, source)
        for box, line, source in zip(boxes, lines, sources, strict=True)
        if _respaced(line) != _respaced(source)
    ]
    if any("\n" in source.strip() for _, _, source in changed):
        return RenderResult(image, "needs_manual", "a box holds more than one line")
    return (
        [box for box, _, _ in changed],
        [line for _, line, _ in changed],
        [original for _, _, original in changed],
    )


def _open_rgba(source: Path) -> Image.Image:
    if source.suffix.lower() in ENCRYPTED_SUFFIXES:
        return Image.open(io.BytesIO(open_bytes(source))).convert("RGBA")
    return Image.open(source).convert("RGBA")


def _find_lettering(
    lum: np.ndarray,
    cores: list[tuple[int, int, int, int]],
    boxes: list[tuple[int, int, int, int]],
    requested_polarity: str,
) -> tuple[list[np.ndarray], list[np.ndarray], np.ndarray]:
    """Each box's lettering and frame, and what was seen there that is not lettering."""
    glyphs = []
    frames = []
    not_lettering = np.zeros(lum.shape, dtype=bool)
    for index, (core, box) in enumerate(zip(cores, boxes, strict=True)):
        polarity = requested_polarity
        if polarity == "auto":
            polarity = detect_polarity(lum, box)
        if box != core:
            others = [*cores[:index], *cores[index + 1 :]]
            found = lettering_in(lum, box, core, polarity, others)
            glyph, frame = found.kept, found.frame
            not_lettering |= found.rejected
        else:
            glyph, frame = build_glyph_mask(lum, box, polarity), np.zeros(lum.shape, bool)
        glyphs.append(glyph)
        frames.append(frame)
    return glyphs, frames, not_lettering


def _erased(
    picture: Image.Image,
    rgb: np.ndarray,
    lum: np.ndarray,
    glyphs: list[np.ndarray],
    not_lettering: np.ndarray,
    inpaint_fn,
) -> Image.Image:
    lettering = np.logical_or.reduce(glyphs)
    mask = dilate(lettering) & ~(not_lettering & ~lettering)
    erased = fill_with_surface(rgb, mask) if is_flat(lum, mask) else inpaint_fn(rgb, mask)
    out = Image.fromarray(erased).convert("RGBA")
    alpha = np.array(picture.getchannel("A"))
    out.putalpha(Image.fromarray(fill_with_surface(alpha, mask)))
    return out


def _fit_line(
    line: str,
    original: str,
    font_path: str,
    glyph: np.ndarray,
    ink: Ink,
    width: int,
    stroke_width: int,
    size: int | None,
) -> FitResult:
    """``line`` sized to the original's ink, in the weight that matches it."""
    height = ink.bbox[3] - ink.bbox[1]
    face = matching_weight(font_path, original, glyph, height)
    if not has_glyphs(face, line):
        face = font_path
    like = original if original.strip() and has_glyphs(face, original) else None
    return fit_font(line, face, height, width, stroke_width, like, size)


def _draw_fitted(
    draw: ImageDraw.ImageDraw,
    ink: Ink,
    line: str,
    fit: FitResult,
    fill: tuple[int, int, int, int] | None,
    stroke: tuple[int, int, int, int] | None,
    stroke_width: int,
) -> None:
    """``line`` centred where the original ink sat."""
    x0, y0, x1, y1 = ink.bbox
    left, top, right, bottom = fit.bbox
    draw.text(
        (
            round((x0 + x1) / 2 - (right - left) / 2 - left),
            round((y0 + y1) / 2 - (bottom - top) / 2 - top),
        ),
        line,
        font=fit.font,
        fill=fill,
        stroke_width=stroke_width,
        stroke_fill=stroke,
    )


def _save_draft(out: Image.Image, destination: Path, source: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()
    if suffix == ".gal":
        save_gal(out, destination)
    elif suffix in ENCRYPTED_SUFFIXES:
        buffer = io.BytesIO()
        out.save(buffer, format="PNG")
        key = key_for(source)
        assert key is not None, f"no encryption key found for {source}"
        destination.write_bytes(encrypt(buffer.getvalue(), key))
    elif suffix in (".jpg", ".jpeg"):
        out.convert("RGB").save(destination, quality=95, subsampling=0)
    else:
        out.save(destination)


def render_image(
    source: Path,
    manifest: dict,
    target: str,
    font_path: str,
    inpaint_fn,
    destination: Path,
    size: int | None = None,
) -> RenderResult:
    """Erase + typeset one picture, writing the draft to ``destination``."""
    order = {**manifest, "image": str(manifest.get("image", source.name))}
    image = order["image"]
    raw_style = order.get("style") or {}
    style = raw_style if isinstance(raw_style, dict) else {}
    gui_layout = _uses_gui_layout(style)
    planned = _lines_to_draw(order, target, gui_layout)
    if isinstance(planned, RenderResult):
        return planned
    cores, lines, originals = planned
    if not cores:
        destination.unlink(missing_ok=True)
        return RenderResult(image, "skipped", "the translation matches the original")
    selected_font = _style_font_path(style, font_path) if gui_layout else font_path
    if not has_glyphs(selected_font, "".join(lines)):
        return RenderResult(image, "needs_manual", "font missing glyphs")

    picture = _open_rgba(source)
    rgb = np.array(picture.convert("RGB"))
    lum = np.array(picture.convert("L"))
    boxes = cores
    if len(cores) > 1 or "regions" in order:
        boxes = [padded_box(box, lum.shape[1], lum.shape[0]) for box in cores]
    polarity = style.get("polarity") or "auto"
    glyphs, frames, not_lettering = _find_lettering(lum, cores, boxes, polarity)
    if not any(glyph.any() for glyph in glyphs):
        return RenderResult(image, "skipped", "no text pixels in region")
    out = _erased(picture, rgb, lum, glyphs, not_lettering, inpaint_fn)
    draw = ImageDraw.Draw(out)

    reasons: list[str] = []
    sizes: list[int] = []
    for box, glyph, frame, line, original in zip(
        boxes, glyphs, frames, lines, originals, strict=True
    ):
        if not glyph.any():
            reasons.append("no text pixels in one box")
            continue
        panel = panel_luminance(lum, box)
        ink = measure_ink(rgb, lum, glyph, panel)
        fill = _rgba(style.get("fill"), ink.fill)
        stroke = _rgba(style.get("stroke"), ink.stroke)
        stroke_width = int(style.get("stroke_width", 1 if stroke else 0))
        if gui_layout:
            text_fill = fill or (*ink.fill, 255)
            if _draw_gui_text(
                out, line, box, style, selected_font, text_fill, stroke, stroke_width
            ):
                reasons.append("text exceeds styled region")
            continue
        width = room(lum, panel, glyph, frame, ink.bbox, box)
        fit = _fit_line(line, original, selected_font, glyph, ink, width, stroke_width, size)
        sizes.append(fit.height_size)
        _draw_fitted(draw, ink, line, fit, fill, stroke, stroke_width)
        if fit.needs_manual:
            reasons.append("shrunk below 60%")
    _save_draft(out, destination, source)

    set_at = sizes[0] if len(sizes) == 1 and len(lines) == 1 else None
    if reasons:
        return RenderResult(image, "needs_manual", reasons[0], set_at)
    return RenderResult(image, "rendered", size=set_at)
