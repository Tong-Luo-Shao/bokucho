from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.proxy_font import (
    JIS_CODE_PAGE_BIT,
    build_proxy_font,
    cp932_repertoire,
    merged_font,
    proxy_family,
    required_characters,
    sample_lines,
    uif_problem,
)
from bokucho.model import Status, StringFile, TextUnit

PROXIES = {"您": "鸙"}


def _open(data: bytes) -> TTFont:
    return TTFont(BytesIO(data))


def _variable_face(path: Path, characters: str) -> Path:
    glyphs = [".notdef", *(f"uni{ord(c):04X}" for c in characters)]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({ord(c): f"uni{ord(c):04X}" for c in characters})
    builder.setupGlyf({glyph: TTGlyphPen(None).glyph() for glyph in glyphs})
    builder.setupHorizontalMetrics({glyph: (500, 0) for glyph in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Var Sans", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.setupFvar(
        axes=[("wght", 100, 400, 900, "Weight")],
        instances=[{"location": {"wght": 700}, "stylename": "Bold"}],
    )
    builder.setupGvar({})
    builder.save(str(path))
    return path


def _cff_face(path: Path, characters: str) -> Path:
    """A face with CFF outlines, as Noto Sans CJK's .otf files have: a square each."""
    glyphs = [".notdef", *(f"uni{ord(c):04X}" for c in characters)]
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({ord(c): f"uni{ord(c):04X}" for c in characters})
    charstrings = {}
    for glyph in glyphs:
        pen = T2CharStringPen(500, None)
        pen.moveTo((100, 0))
        pen.lineTo((100, 500))
        pen.lineTo((400, 500))
        pen.lineTo((400, 0))
        pen.closePath()
        charstrings[glyph] = pen.getCharString()
    builder.setupCFF("CFFSans", {"FullName": "CFF Sans"}, charstrings, {})
    builder.setupHorizontalMetrics({glyph: (500, 0) for glyph in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "CFF Sans", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.save(str(path))
    return path


def test_each_donor_draws_the_character_it_stands_for(tmp_path: Path, make_font) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "您好說鸙")
    proxy = build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好")
    cmap = _open(proxy.data).getBestCmap()
    assert cmap[ord("鸙")] == cmap[ord("您")]
    assert proxy.undrawable == ""


def test_a_face_with_cff_outlines_is_built_with_truetype_ones(tmp_path: Path) -> None:
    source = _cff_face(tmp_path / "source.otf", "您好鸙")
    font = _open(build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好").data)
    assert font.sfntVersion == "\0\1\0\0"
    assert "CFF " not in font
    cmap = font.getBestCmap()
    assert cmap[ord("鸙")] == cmap[ord("您")]
    assert font["glyf"][cmap[ord("您")]].numberOfContours == 1


def test_only_what_cp932_can_ask_for_is_kept(tmp_path: Path, make_font) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "您好說鸙")
    proxy = build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好")
    cmap = _open(proxy.data).getBestCmap()
    assert ord("好") in cmap
    assert ord("說") not in cmap


def test_the_face_goes_by_the_family_and_claims_only_the_japanese_code_page(
    tmp_path: Path, make_font
) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "您好鸙")
    claims = TTFont(source)
    claims["OS/2"].ulCodePageRange1 = 1 << 0 | 1 << 20
    claims["OS/2"].ulCodePageRange2 = 1 << 31
    claims.save(source)
    font = _open(build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好").data)
    names = font["name"]
    for name_id in (1, 3, 4, 6):
        assert names.getDebugName(name_id) == "Bokucho-a1b2c3"
    assert names.getDebugName(2) == "Regular"
    assert all("Source" not in record.toUnicode() for record in names.names)
    assert font["OS/2"].ulCodePageRange1 == 1 << JIS_CODE_PAGE_BIT
    assert font["OS/2"].ulCodePageRange2 == 0


def test_the_face_fills_the_cell_the_game_asks_for(tmp_path: Path, make_font) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "您好鸙")
    tall = TTFont(source)
    tall["OS/2"].sTypoAscender, tall["OS/2"].sTypoDescender = 880, -120
    tall["OS/2"].usWinAscent, tall["OS/2"].usWinDescent = 1160, 288
    tall["hhea"].ascent, tall["hhea"].descent = 1000, -200
    tall.save(source)
    font = _open(build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好").data)
    assert (font["OS/2"].usWinAscent, font["OS/2"].usWinDescent) == (880, 120)
    assert (font["hhea"].ascent, font["hhea"].descent) == (880, -120)


def test_a_face_without_typo_metrics_is_split_as_its_hhea_splits_it(
    tmp_path: Path, make_font
) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "您好鸙")
    font = _open(build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好").data)
    assert (font["OS/2"].usWinAscent, font["OS/2"].usWinDescent) == (800, 200)


def test_a_character_the_source_lacks_is_named(tmp_path: Path, make_font) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "好鸙")
    proxy = build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好")
    assert proxy.undrawable == "您"


def test_a_cp932_character_the_source_lacks_is_named_too(tmp_path: Path, make_font) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "您鸙")
    proxy = build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="您好")
    assert proxy.undrawable == "好"


def test_a_pair_no_translation_needs_is_not_asked_of_the_face(tmp_path: Path, make_font) -> None:
    source = make_font(tmp_path / "source.ttf", "Source Sans", "好鸙")
    proxy = build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", required="好")
    assert proxy.undrawable == ""
    assert ord("鸙") in _open(proxy.data).getBestCmap()


def test_a_variable_face_is_pinned_to_the_instance_asked_for(tmp_path: Path) -> None:
    source = _variable_face(tmp_path / "variable.ttf", "您好鸙")
    font = _open(build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", "Bold", required="您好").data)
    assert "fvar" not in font
    with pytest.raises(ValueError, match="Heavy"):
        build_proxy_font(source, PROXIES, "Bokucho-a1b2c3", "Heavy", required="您好")


def test_the_family_is_cut_to_the_default_it_replaces() -> None:
    family = proxy_family(PROXIES, b"face bytes", "", 13)
    assert family.startswith("Bokucho-") and len(family) == 13
    assert proxy_family(PROXIES, b"face bytes", "", 13) == family
    assert proxy_family({"您": "鸙", "說": "鶴"}, b"face bytes", "", 13) != family
    with pytest.raises(ValueError, match="10"):
        proxy_family(PROXIES, b"face bytes", "", 9)


def test_a_variables_name_asks_nothing_of_the_face(tmp_path: Path) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path / "game", "測試", "ja", "zh-Hant", "livemaker")
    )
    units = [
        TextUnit(id="u1", source="<VAR 値>です", target="您好<VAR 値>", status=Status.REVIEWED),
        TextUnit(id="u2", source="値<VAR 数>", target="値<VAR 数>", status=Status.REVIEWED),
    ]
    project.save_string_file(StringFile(source_file="a.lsb", units=units), "a.lsb.json")
    assert set(required_characters(project)) == set("您好値")


def test_the_font_is_previewed_with_the_first_lines_as_the_game_draws_them(
    tmp_path: Path,
) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path / "game", "測試", "ja", "zh-Hant", "livemaker")
    )
    units = [
        TextUnit(id="u1", source="未訳", target="", status=Status.UNTRANSLATED),
        TextUnit(id="u2", source="<VAR 値>です", target="傷害<VAR 値>點", status=Status.MT),
        TextUnit(id="u3", source="空白", target="  第一行\n  第二行  ", status=Status.MT),
        TextUnit(id="u4", source="三", target="第三句", status=Status.MT),
    ]
    project.save_string_file(StringFile(source_file="a.lsb", units=units), "a.lsb.json")
    assert sample_lines(project, 2) == ["傷害點", "第一行 第二行"]
    repertoire = cp932_repertoire()
    assert ord("好") in repertoire and ord("鸙") in repertoire
    assert ord("您") not in repertoire


def _dll(machine: int, body: bytes = b"") -> bytes:
    """The bytes of a DLL as far as a loader's first look reads them."""
    header = 0x40
    return b"MZ" + bytes(header - 6) + header.to_bytes(4, "little") + b"PE\0\0" + (
        machine.to_bytes(2, "little") + body
    )


@pytest.mark.parametrize(
    ("dll", "problem"),
    [
        (_dll(0x14C, b"..uif_config.json.."), None),
        (_dll(0x8664, b"..uif_config.json.."), "not 32-bit"),
        (_dll(0x14C, b"..timeGetTime.."), "not UIF"),
        (b"MZ\x90\x00 uif", "not a Windows DLL"),
    ],
)
def test_only_a_32_bit_uif_loads_the_proxy_font(dll: bytes, problem: str | None) -> None:
    found = uif_problem(dll)
    assert found == problem if problem is None else problem in found


def _square_font(path: Path, upem: int, characters: str, size: int, advance: int) -> Path:
    """A TrueType face with a ``size`` square for each character, ``advance`` wide."""
    glyphs = [".notdef", *(f"u{ord(c):04X}" for c in characters)]
    builder = FontBuilder(upem, isTTF=True)
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({ord(c): f"u{ord(c):04X}" for c in characters})
    outlines = {}
    for glyph in glyphs:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((0, size))
        pen.lineTo((size, size))
        pen.lineTo((size, 0))
        pen.closePath()
        outlines[glyph] = pen.glyph()
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({glyph: (advance, 0) for glyph in glyphs})
    builder.setupHorizontalHeader(ascent=upem * 4 // 5, descent=-upem // 5)
    builder.setupNameTable({"familyName": "Square", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.save(str(path))
    return path


def _reloaded(font: TTFont) -> TTFont:
    out = BytesIO()
    font.save(out)
    return _open(out.getvalue())


def _coordinates(font: TTFont, glyph: str) -> list[tuple[int, int]]:
    return [tuple(point) for point in font["glyf"][glyph].getCoordinates(font["glyf"])[0]]


def _square(size: int) -> list[tuple[int, int]]:
    return [(0, 0), (0, size), (size, size), (size, 0)]


def test_the_fill_adds_only_what_the_base_lacks(tmp_path: Path) -> None:
    base = TTFont(_square_font(tmp_path / "base.ttf", 4096, "日A", 3000, 4096))
    fill = TTFont(_square_font(tmp_path / "fill.ttf", 4096, "日檔", 2000, 4096))
    merged = _reloaded(merged_font(base, fill, "Bokucho Base"))
    cmap = merged.getBestCmap()
    assert _coordinates(merged, cmap[ord("日")]) == _square(3000)
    assert _coordinates(merged, cmap[ord("檔")]) == _square(2000)
    assert merged["hmtx"][cmap[ord("檔")]][0] == 4096
    assert ord("A") in cmap
    assert merged["name"].getDebugName(1) == "Bokucho Base"
    assert merged["OS/2"].ulCodePageRange1 & (1 << 20)


def test_a_character_beyond_the_bmp_gets_a_long_cmap(tmp_path: Path) -> None:
    base = TTFont(_square_font(tmp_path / "base.ttf", 1000, "日", 800, 1000))
    fill = TTFont(_square_font(tmp_path / "fill.ttf", 1000, "\U00020000", 800, 1000))
    merged = _reloaded(merged_font(base, fill, "Bokucho Base"))
    assert 0x20000 in merged.getBestCmap()


def test_the_shortest_proxy_family_still_fits_exactly() -> None:
    family = proxy_family(PROXIES, b"face bytes", "", 10)
    assert family.startswith("Bokucho-") and len(family) == 10
