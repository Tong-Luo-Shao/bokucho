"""The font an RGSS game draws its text in, the characters it lacks, and a face to fill them."""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from fontTools import subset
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.areaPen import AreaPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTCollection, TTFont
from fontTools.varLib import instancer

import bokucho
from bokucho.appdirs import bundled_font_dir, portable_root
from bokucho.engines.rpgmaker_scripts import load_scripts
from bokucho.engines.ruby_literals import UnreadableScript, literals
from bokucho.infrastructure.installed_fonts import FONT_SUFFIXES, font_dirs

_log = logging.getLogger(__name__)

#: Kanji to measure a face by: common, and drawn by Japanese and Chinese faces alike.
REFERENCE = "日本語国家時間電話学校会社東京大阪見聞読書道路森林海空雨雪"
#: The face added characters come from for each style, and its license among Bokucho's.
DONORS = {
    "gothic": ("Noto Sans TC", "Noto-Sans-CJK-OFL.txt"),
    "mincho": ("GenYoMin2 TW", "GenYo-GenWan-OFL.txt"),
}
_MINCHO = re.compile(r"(?i)明朝|mincho|serif|\bmin\b")
#: The weights a variable donor is tried at: wide enough for any text face's kanji.
WEIGHTS = range(250, 701, 25)


@dataclass(frozen=True)
class FontFill:
    """What a patch carries so the game's own font draws every translated character."""

    base: str
    face: int
    sha256: str
    family: str
    file: str
    fill: bytes
    license: bytes
    characters: str
    donor: str
    weight: int | None


def script_strings(blob: bytes) -> list[str]:
    """Every string literal of every readable script, scripts and literals in order."""
    strings: list[str] = []
    for entry in load_scripts(blob).entries:
        if entry.source is None:
            continue
        try:
            strings += [literal.text for literal in literals(entry.source)]
        except UnreadableScript:
            continue
    return strings


def family_names(font: TTFont) -> set[str]:
    return {
        record.toUnicode(errors="replace")
        for record in font["name"].names
        if record.nameID in (1, 16)
    }


def style(font: TTFont) -> str:
    """``"mincho"`` when the face's family names say so, else ``"gothic"``."""
    return "mincho" if any(_MINCHO.search(name) for name in family_names(font)) else "gothic"


def missing_characters(font: TTFont, texts: Iterable[str]) -> str:
    """The non-ASCII characters of ``texts`` that ``font`` does not map, sorted."""
    mapped = font.getBestCmap()
    return "".join(
        sorted(
            {
                character
                for text in texts
                for character in text
                if ord(character) > 0x7F
                and character.isprintable()
                and not character.isspace()
                and ord(character) not in mapped
            }
        )
    )


def _faces(paths: Iterable[Path]) -> Iterator[tuple[Path, int, set[str]]]:
    """Each face of each font file among ``paths``, with its family names."""
    for path in paths:
        if path.suffix.lower() not in FONT_SUFFIXES or not path.is_file():
            continue
        try:
            count = 1
            if path.suffix.lower() == ".ttc":
                with TTCollection(path, lazy=True) as collection:
                    count = len(collection.fonts)
            for face in range(count):
                with TTFont(path, fontNumber=face, lazy=True) as font:
                    yield path, face, family_names(font)
        except Exception:  # noqa: BLE001 - a broken file among the fonts is skipped
            _log.debug("Not a font that opens: %s", path)


def choose_base(game_root: Path, strings: Sequence[str]) -> tuple[Path, int] | None:
    """The file and face of the font the game ships for its text, or None."""
    folder = None
    if game_root.is_dir():
        folder = next(
            (c for c in sorted(game_root.iterdir()) if c.is_dir() and c.name.lower() == "fonts"),
            None,
        )
    faces = list(_faces(sorted(folder.iterdir()))) if folder else []
    if len(faces) == 1:
        return faces[0][0], faces[0][1]
    if not faces:
        _log.warning("Font fill skipped: the game ships no font in a Fonts folder.")
        return None
    for string in strings:
        for path, face, names in faces:
            if string in names:
                return path, face
    shipped = ", ".join(sorted({name for _, _, names in faces for name in names}))
    _log.warning(
        "Font fill skipped: the game ships %d fonts (%s) and no script names one.",
        len(faces),
        shipped,
    )
    return None


def donor_dirs() -> list[Path]:
    """Where a donor face may be: inside Bokucho, beside it, then this machine's fonts."""
    inside = Path(bokucho.__file__).parent / "web" / "assets" / "fonts"
    return [inside, bundled_font_dir(), *font_dirs()]


def find_donor(family: str) -> tuple[Path, int] | None:
    """The first file and face in ``donor_dirs()`` that goes by ``family``."""
    for folder in donor_dirs():
        if not folder.is_dir():
            continue
        for path, face, names in _faces(sorted(folder.rglob("*"))):
            if family in names:
                return path, face
    return None


def fill_names(base_family: str) -> tuple[str, str]:
    """The family and file name of the copy: plain ASCII, so a Ruby string can name them, and
    at most 31 characters, all Windows matches a face name by."""
    plain = " ".join(re.sub(r"[^A-Za-z0-9 ]", "", base_family).split())[:23].rstrip() or "Font"
    return f"Bokucho {plain}", f"Bokucho-{plain.replace(' ', '')}.ttf"


def _ink(font: TTFont, characters: str) -> dict[str, tuple[float, float, float]]:
    """Ink height, ink centre and ink density of each character ``font`` draws, in its em."""
    glyphs, cmap = font.getGlyphSet(), font.getBestCmap()
    upem = font["head"].unitsPerEm
    found = {}
    for character in characters:
        name = cmap.get(ord(character))
        if name is None:
            continue
        bounds, area = BoundsPen(glyphs), AreaPen(glyphs)
        glyphs[name].draw(bounds)
        glyphs[name].draw(area)
        if bounds.bounds is None:
            continue
        _, bottom, _, top = bounds.bounds
        found[character] = (
            (top - bottom) / upem,
            (top + bottom) / 2 / upem,
            abs(area.value) / upem**2,
        )
    return found


def _mean(ink: dict[str, tuple[float, float, float]], shared: list[str]) -> tuple[float, ...]:
    return tuple(sum(ink[c][i] for c in shared) / len(shared) for i in range(3))


def build_fill(base: TTFont, donor: TTFont, characters: str) -> tuple[bytes, int | None]:
    """A TrueType font in ``base``'s units drawing ``characters`` as ``donor`` draws them.

    The donor's kanji are scaled and shifted onto the base's, and a variable donor is pinned at
    the weight whose ink density is closest to the base's. Returns the font and that weight.
    Cuts ``donor`` down in place.
    """
    options = subset.Options()
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_outline = True
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes={ord(c) for c in REFERENCE + characters})
    subsetter.subset(donor)

    base_upem = base["head"].unitsPerEm
    base_ink = _ink(base, REFERENCE)
    weight, instance, factor, shift = _closest_instance(donor, base_ink, base_upem)
    base_cmap = base.getBestCmap()
    advances = Counter(base["hmtx"][base_cmap[ord(c)]][0] for c in REFERENCE if c in base_ink)
    advance = advances.most_common(1)[0][0] if advances else base_upem
    scale = factor * base_upem / instance["head"].unitsPerEm
    outlines, metrics, mapping = _fill_glyphs(instance, characters, advance, scale, shift)

    builder = FontBuilder(base_upem, isTTF=True)
    builder.setupGlyphOrder(list(outlines))
    builder.setupCharacterMap(mapping)
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=base["hhea"].ascent, descent=base["hhea"].descent)
    names = {name_id: donor["name"].getDebugName(name_id) for name_id in (0, 13, 14)}
    builder.setupNameTable(
        {"familyName": "Bokucho Fill", "styleName": "Regular"}
        | {name_id: value for name_id, value in names.items() if value}
    )
    builder.setupOS2()
    builder.setupPost(keepGlyphNames=False)
    out = BytesIO()
    builder.save(out)
    return out.getvalue(), weight


def _weight_candidates(
    donor: TTFont, base_ink: dict[str, tuple[float, float, float]]
) -> list[tuple[dict[str, float] | None, int | None]]:
    """The donor as it is, or for a variable donor each weight it can be pinned at."""
    if "fvar" not in donor:
        return [(None, None)]
    axes = {axis.axisTag: axis for axis in donor["fvar"].axes}
    defaults = {tag: axis.defaultValue for tag, axis in axes.items()}
    wght = axes.get("wght")
    weights = [w for w in WEIGHTS if wght and wght.minValue <= w <= wght.maxValue]
    candidates = [({**defaults, "wght": w}, w) for w in weights]
    if not candidates or not set(base_ink) & set(_ink(donor, REFERENCE)):
        return [(defaults, round(wght.defaultValue) if wght else None)]
    return candidates


def _closest_instance(
    donor: TTFont, base_ink: dict[str, tuple[float, float, float]], base_upem: int
) -> tuple[int | None, TTFont, float, float]:
    """The donor weight whose ink density is nearest the base's, with the scale and shift
    that put its kanji onto the base's."""
    best = None
    for location, weight in _weight_candidates(donor, base_ink):
        instance = instancer.instantiateVariableFont(donor, location) if location else donor
        ink = _ink(instance, REFERENCE)
        shared = [c for c in REFERENCE if c in ink and c in base_ink]
        if not shared:
            return weight, instance, 1.0, 0.0
        height, centre, density = _mean(base_ink, shared)
        donor_height, donor_centre, donor_density = _mean(ink, shared)
        factor = height / donor_height
        shift = (centre - donor_centre * factor) * base_upem
        score = abs(donor_density * factor**2 - density)
        if best is None or score < best[0]:
            best = (score, weight, instance, factor, shift)
    _, weight, instance, factor, shift = best
    return weight, instance, factor, shift


def _fill_glyphs(
    instance: TTFont, characters: str, advance: int, scale: float, shift: float
) -> tuple[dict, dict, dict]:
    """Each character the donor draws, as a quadratic outline on the base's grid."""
    cubic = "CFF " in instance or "CFF2" in instance
    glyphs, cmap = instance.getGlyphSet(), instance.getBestCmap()
    outlines = {".notdef": TTGlyphPen(None).glyph()}
    metrics = {".notdef": (advance, 0)}
    mapping = {}
    for character in characters:
        source = cmap.get(ord(character))
        if source is None:
            continue
        code = ord(character)
        name = f"uni{code:04X}" if code <= 0xFFFF else f"u{code:05X}"
        dx = (advance - scale * instance["hmtx"][source][0]) / 2
        recording = DecomposingRecordingPen(glyphs)
        glyphs[source].draw(recording)
        pen = TTGlyphPen(None)
        quadratic = Cu2QuPen(pen, max_err=1.0, reverse_direction=cubic)
        recording.replay(TransformPen(quadratic, (scale, 0, 0, scale, dx, shift)))
        glyph = pen.glyph()
        glyph.recalcBounds(None)
        outlines[name] = glyph
        metrics[name] = (advance, getattr(glyph, "xMin", 0))
        mapping[code] = name
    return outlines, metrics, mapping


def plan_font_fill(texts: Iterable[str], game_root: Path, scripts: bytes | None) -> FontFill | None:
    """What a patch carries to draw ``texts`` in the game's own font, or None when nothing is."""
    found = choose_base(game_root, script_strings(scripts) if scripts is not None else [])
    if found is None:
        return None
    path, face = found
    where = path.relative_to(game_root).as_posix()
    with TTFont(path, fontNumber=face) as base:
        if "glyf" not in base:
            _log.warning("Font fill skipped: %s has CFF outlines; Bokucho adds to TrueType.", where)
            return None
        characters = missing_characters(base, texts)
        if not characters:
            return None
        donor_family, license_name = DONORS[style(base)]
        donor_at = find_donor(donor_family)
        if donor_at is None:
            _log.warning(
                "Font fill skipped: %s lacks %d characters, and %s to take them from is neither "
                "among Bokucho's fonts nor installed.",
                where,
                len(characters),
                donor_family,
            )
            return None
        try:
            license = (portable_root() / "licenses" / license_name).read_bytes()
        except OSError:
            _log.warning(
                "Font fill skipped: the license of %s is not beside Bokucho.", donor_family
            )
            return None
        with TTFont(donor_at[0], fontNumber=donor_at[1]) as donor:
            characters = _drawn_by(donor, characters, where, donor_family)
            if not characters:
                return None
            fill, weight = build_fill(base, donor, characters)
        family, file = fill_names(base["name"].getBestFamilyName() or "")
    _log.info("Font fill: %d characters from %s into %s", len(characters), donor_family, where)
    return FontFill(
        base=where,
        face=face,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        family=family,
        file=file,
        fill=fill,
        license=license,
        characters=characters,
        donor=donor_family,
        weight=weight,
    )


def _drawn_by(donor: TTFont, characters: str, where: str, donor_family: str) -> str:
    drawn = donor.getBestCmap()
    lost = "".join(c for c in characters if ord(c) not in drawn)
    if lost:
        _log.warning(
            "Characters neither %s nor %s draws, which stay boxes: %s",
            where,
            donor_family,
            lost,
        )
    return "".join(c for c in characters if ord(c) in drawn)
