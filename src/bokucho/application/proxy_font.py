"""A face the game asks for by name, drawing each donor as the character it stands for."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject, ReleaseBlocked
from bokucho.application.projects import resolve_game_path
from bokucho.engines.cp932_proxy import ProxyMap
from bokucho.engines.livemaker import PROJECT_SETTINGS, replace_status_font, status_font_name
from bokucho.project import Project
from bokucho.qa import without_markup

try:
    from fontTools import subset
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont, newTable
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
    from fontTools.varLib import instancer

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _IMPORT_ERROR = exc

PREFIX = "Bokucho-"
MIN_FAMILY_LENGTH = 10
JIS_CODE_PAGE_BIT = 17
BIG5_CODE_PAGE_BIT = 20
FAMILY_NAME_IDS = (1, 2, 3, 4, 6, 16, 17, 21, 22)


@dataclass(frozen=True, slots=True)
class ProxyFont:
    data: bytes
    undrawable: str


@lru_cache(maxsize=1)
def cp932_repertoire() -> frozenset[int]:
    """Every code point CP932 encodes: all a CP932 script can ask a face for."""
    codes = set()
    for code in range(0x20, 0x10000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        try:
            chr(code).encode("cp932")
        except UnicodeEncodeError:
            continue
        codes.add(code)
    return frozenset(codes)


def proxy_family(real2proxy: dict[str, str], source: bytes, variation: str, length: int) -> str:
    """``Bokucho-`` and a digest of what the face is made from, ``length`` bytes long."""
    if length < MIN_FAMILY_LENGTH:
        raise ValueError(
            f"The game's default face name is {length} bytes; a proxy name needs at "
            f"least {MIN_FAMILY_LENGTH}."
        )
    digest = hashlib.sha256()
    for real, proxy in sorted(real2proxy.items()):
        digest.update(f"{real}{proxy}".encode())
    digest.update(variation.encode())
    digest.update(source)
    # ponytail: names longer than 72 bytes are not met by any game seen; extend the digest then.
    return (PREFIX + digest.hexdigest())[:length]


def required_characters(project: Project) -> str:
    """The characters the game text font must draw: those of the non-image translations."""
    engine = project.config.engine.type
    return "".join(
        sorted(
            {
                character
                for string_file in project.load_string_files()
                for unit in string_file.units
                if unit.kind != "image"
                for character in without_markup(unit.target, engine)
                if not character.isspace()
            }
        )
    )


def sample_lines(project: Project, count: int = 3) -> list[str]:
    """The first ``count`` translated lines as the game text font draws them, to preview it."""
    engine = project.config.engine.type
    lines: list[str] = []
    for string_file in project.load_string_files():
        for unit in string_file.units:
            if unit.kind == "image":
                continue
            line = " ".join(without_markup(unit.target, engine).split())
            if line:
                lines.append(line)
                if len(lines) == count:
                    return lines
    return lines


def build_proxy_font(
    source: Path,
    real2proxy: dict[str, str],
    family: str,
    variation: str = "",
    *,
    required: str,
) -> ProxyFont:
    """The proxy face built from ``source``; ``undrawable`` is what of ``required`` it lacks."""
    if _IMPORT_ERROR is not None:
        raise ImportError("Building a proxy face needs fontTools.") from _IMPORT_ERROR
    font = TTFont(source, fontNumber=0)
    if "fvar" in font:
        font = pinned(font, variation, source)
    options = subset.Options()
    options.layout_features = ["*"]
    options.name_IDs = ["*"]
    options.name_languages = ["*"]
    options.notdef_outline = True
    source_cmap = font.getBestCmap()
    subsetter = subset.Subsetter(options)
    subsetter.populate(unicodes=cp932_repertoire() | {ord(real) for real in real2proxy})
    subsetter.subset(font)
    glyphs = font.getBestCmap()
    undrawable = "".join(
        sorted({c for c in required if not c.isspace() and ord(c) not in source_cmap})
    )
    for table in font["cmap"].tables:
        if getattr(table, "format", None) not in (4, 12):
            continue
        for real, proxy in real2proxy.items():
            glyph = glyphs.get(ord(real))
            if glyph is not None:
                table.cmap[ord(proxy)] = glyph
    rename_family(font, family)
    font["OS/2"].ulCodePageRange1 = 1 << JIS_CODE_PAGE_BIT
    font["OS/2"].ulCodePageRange2 = 0
    os2, upm = font["OS/2"], font["head"].unitsPerEm
    top, bottom = os2.sTypoAscender, -os2.sTypoDescender
    if top + bottom <= 0:
        top, bottom = font["hhea"].ascent, -font["hhea"].descent
    ascent = round(upm * top / (top + bottom))
    os2.usWinAscent, os2.usWinDescent = ascent, upm - ascent
    font["hhea"].ascent, font["hhea"].descent = ascent, ascent - upm
    if "CFF " in font or "CFF2" in font:
        _to_truetype(font)
    out = BytesIO()
    font.save(out)
    return ProxyFont(out.getvalue(), undrawable)


def _to_truetype(font: TTFont) -> None:
    """Redraw ``font``'s CFF outlines as TrueType ones."""
    glyph_set = font.getGlyphSet()
    glyf = newTable("glyf")
    glyf.glyphOrder = font.getGlyphOrder()
    glyf.glyphs = {}
    for name in glyf.glyphOrder:
        pen = TTGlyphPen(None)
        glyph_set[name].draw(Cu2QuPen(pen, max_err=1.0, reverse_direction=True))
        glyf[name] = pen.glyph()
    for tag in ("CFF ", "CFF2", "VORG"):
        if tag in font:
            del font[tag]
    font["glyf"] = glyf
    font["loca"] = newTable("loca")
    font["head"].glyphDataFormat = 0
    maxp = font["maxp"]
    maxp.tableVersion = 0x00010000
    maxp.maxZones = 1
    for field in (
        "maxTwilightPoints",
        "maxStorage",
        "maxFunctionDefs",
        "maxInstructionDefs",
        "maxStackElements",
        "maxSizeOfInstructions",
        "maxComponentElements",
    ):
        setattr(maxp, field, 0)
    font["post"].formatType = 3.0
    font.sfntVersion = "\0\1\0\0"


def pinned(font: TTFont, variation: str, source: Path) -> TTFont:
    """A variable face fixed at the named instance asked for, or at its defaults."""
    location = {axis.axisTag: axis.defaultValue for axis in font["fvar"].axes}
    if variation:
        names = font["name"]
        for instance in font["fvar"].instances:
            if names.getDebugName(instance.subfamilyNameID) == variation:
                location = dict(instance.coordinates)
                break
        else:
            raise ValueError(f"{variation!r} is not a named instance of {source.name}.")
    return instancer.instantiateVariableFont(font, location)


def rename_family(font: TTFont, family: str) -> None:
    names = font["name"]
    for name_id in FAMILY_NAME_IDS:
        names.removeNames(nameID=name_id)
    for name_id, value in ((1, family), (2, "Regular"), (3, family), (4, family), (6, family)):
        names.setName(value, name_id, 3, 1, 0x409)


def merged_font(base: TTFont, fill: TTFont, family: str) -> TTFont:
    """``base`` drawing every character of ``fill`` it lacks, renamed to ``family``.

    ``fill`` is already in ``base``'s units: its outlines and advances are copied as they are.
    """
    glyf, hmtx = base["glyf"], base["hmtx"]
    mapped = base.getBestCmap()
    names = set(base.getGlyphOrder())
    fill_glyphs = fill.getGlyphSet()
    ascent = base["hhea"].ascent
    added: dict[int, str] = {}
    for code, source in sorted(fill.getBestCmap().items()):
        if code in mapped:
            continue
        name = source if source not in names else f"{source}.bokucho"
        recording = DecomposingRecordingPen(fill_glyphs)
        fill_glyphs[source].draw(recording)
        pen = TTGlyphPen(None)
        recording.replay(pen)
        glyph = pen.glyph()
        glyph.recalcBounds(glyf)
        glyf[name] = glyph
        hmtx[name] = (fill["hmtx"][source][0], getattr(glyph, "xMin", 0))
        if "vmtx" in base:
            top = ascent - getattr(glyph, "yMax", ascent)
            base["vmtx"][name] = (base["head"].unitsPerEm, top)
        names.add(name)
        added[code] = name
    base.setGlyphOrder(list(glyf.glyphOrder))
    # Per-glyph device caches; optional, and stale once glyphs are added.
    for tag in ("hdmx", "LTSH"):
        if tag in base:
            del base[tag]
    tables = base["cmap"].tables
    for table in tables:
        if table.isUnicode() and table.format in (4, 12):
            table.cmap.update(
                (code, name) for code, name in added.items() if table.format == 12 or code <= 0xFFFF
            )
    beyond = {code: name for code, name in added.items() if code > 0xFFFF}
    if beyond and not any(table.format == 12 and table.isUnicode() for table in tables):
        long = CmapSubtable.newSubtable(12)
        long.platformID, long.platEncID, long.language = 3, 10, 0
        long.cmap = {**base.getBestCmap(), **beyond}
        tables.append(long)
    base["OS/2"].ulCodePageRange1 |= 1 << BIG5_CODE_PAGE_BIT
    rename_family(base, family)
    return base


_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()


@dataclass(frozen=True, slots=True)
class SystemFontResult:
    font: Path
    family: str
    scripts: int
    slots: int
    archive: Path


def patch_system_font(
    project: Project,
    adapter_factory,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    *,
    out_dir: Path | None = None,
) -> SystemFontResult:
    """Build the proxy face, point the game at it, and rebuild the archive."""
    adapter = adapter_factory(project)
    overlay = project.root / "out"
    map_path, source, source_path = _system_font_inputs(project, overlay)
    real2proxy = ProxyMap.load(map_path).real2proxy
    game_path = resolve_game_path(project)
    settings = adapter.project_settings(game_path)
    try:
        default = status_font_name(settings)
        family = proxy_family(real2proxy, source_path.read_bytes(), source.variation, len(default))
    except ValueError as error:
        raise ReleaseBlocked(str(error)) from error
    sink.publish(ProgressEvent("font", 1, 3, "font.proxy.named", detail=family))
    token.raise_if_cancelled()

    proxy = build_proxy_font(
        source_path, real2proxy, family, source.variation, required=required_characters(project)
    )
    if proxy.undrawable:
        raise ReleaseBlocked(
            f"The game text font draws none of {proxy.undrawable}; choose one that "
            "draws every translated character."
        )
    dist = out_dir or project.root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    font_path = dist / f"{family}.ttf"
    font_path.write_bytes(proxy.data)
    (overlay / PROJECT_SETTINGS).write_bytes(replace_status_font(settings, family))
    sink.publish(ProgressEvent("font", 2, 3, "font.proxy.built", detail=font_path.name))
    token.raise_if_cancelled()

    adapter.start_with_face(overlay, settings, family, sink, token)
    scripts, slots = adapter.retarget_fonts(overlay, family, sink, token)
    archive = adapter.repack(game_path, overlay, dist, sink, token)
    sink.publish(ProgressEvent("font", 3, 3, "font.archive.rebuilt", detail=str(archive)))
    return SystemFontResult(font_path, family, scripts, slots, archive)


def _system_font_inputs(project: Project, overlay: Path):
    """The proxy map, the chosen font source and its path; refuses when one is missing."""
    if not any(overlay.rglob("*.lsb")):
        raise InvalidProject("No overlay to patch. Run `bokucho inject` first.")
    map_path = project.root / project.config.engine.proxy_map
    if not map_path.is_file():
        raise InvalidProject("No proxy map. Run `bokucho patch-runtime` first.")
    if not project.config.font.sources:
        raise InvalidProject("Choose the game text font on the release screen first.")
    source = project.config.font.sources[0]
    source_path = project.root / source.path
    if not source_path.is_file():
        raise InvalidProject(f"Font source not found: {source_path}")
    return map_path, source, source_path


UIF_CONFIG = "uif_config.json"
UIF_DLL = "winmm.dll"
_I386 = 0x14C


def uif_problem(dll: bytes) -> str | None:
    """Why ``dll`` is no UIF winmm.dll a LiveMaker game loads, None if it is one."""
    header = int.from_bytes(dll[0x3C:0x40], "little") if dll[:2] == b"MZ" else 0
    if not header or dll[header : header + 4] != b"PE\0\0":
        return "not a Windows DLL"
    if int.from_bytes(dll[header + 4 : header + 6], "little") != _I386:
        return "not 32-bit, as LiveMaker games are"
    if UIF_CONFIG.encode() not in dll:
        return f"not UIF: it never reads {UIF_CONFIG}"
    return None


def uif_config(font_file: str, family: str) -> str:
    """UIF's configuration: load the proxy beside the game privately, spoof nothing."""
    return json.dumps(
        {
            "injector": {"enable": True, "print_loaded_modules": False},
            "allocate_console": False,
            "font_manager": {
                "enable": True,
                "resource_files": [font_file],
                "spoof_enumeration": {
                    "enable": False,
                    "filter_pitch_and_family": 0,
                    "filter_charset": 1,
                    "spoof_charset": 128,
                },
                "spoof_creation": {
                    "enable": False,
                    "override_face": family,
                    "override_charset": 0,
                },
            },
        },
        ensure_ascii=False,
        indent=1,
    )
