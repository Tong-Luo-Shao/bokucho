from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.TupleVariation import TupleVariation
from fontTools.ttLib.ttCollection import TTCollection

from bokucho.appdirs import portable_root
from bokucho.application import rgss_font
from bokucho.application.rgss_font import (
    build_fill,
    choose_base,
    fill_names,
    find_donor,
    missing_characters,
    plan_font_fill,
    script_strings,
    style,
)


def test_the_only_shipped_font_is_the_base(tmp_path: Path, make_font) -> None:
    path = make_font(tmp_path / "game" / "Fonts" / "ume.ttf", "UmePlus Gothic", "日")
    assert choose_base(tmp_path / "game", []) == (path, 0)


def test_a_lowercase_fonts_folder_is_found(tmp_path: Path, make_font) -> None:
    path = make_font(tmp_path / "game" / "fonts" / "vl.ttf", "VL Gothic", "日")
    assert choose_base(tmp_path / "game", []) == (path, 0)


def test_of_several_the_one_the_scripts_name_first_is_the_base(tmp_path: Path, make_font) -> None:
    make_font(tmp_path / "game" / "Fonts" / "a.ttf", "Alpha", "日")
    beta = make_font(tmp_path / "game" / "Fonts" / "b.ttf", "Beta", "日")
    assert choose_base(tmp_path / "game", ["ＭＳ Ｐ明朝", "Beta", "Alpha"]) == (beta, 0)


def test_a_face_of_a_collection_can_be_the_base(tmp_path: Path, make_font) -> None:
    collection = TTCollection()
    collection.fonts = [
        TTFont(make_font(tmp_path / "a.ttf", "Alpha", "日")),
        TTFont(make_font(tmp_path / "b.ttf", "Beta", "日")),
    ]
    game = tmp_path / "game"
    (game / "Fonts").mkdir(parents=True)
    collection.save(game / "Fonts" / "collection.ttc")
    assert choose_base(game, ["Beta"]) == (game / "Fonts" / "collection.ttc", 1)


def test_no_base_when_nothing_is_shipped_or_named(tmp_path: Path, make_font) -> None:
    assert choose_base(tmp_path / "empty", []) is None
    make_font(tmp_path / "game" / "Fonts" / "a.ttf", "Alpha", "日")
    make_font(tmp_path / "game" / "Fonts" / "b.ttf", "Beta", "日")
    assert choose_base(tmp_path / "game", ["Gamma"]) is None


def test_missing_characters_are_what_the_font_does_not_map(tmp_path: Path, make_font) -> None:
    font = TTFont(make_font(tmp_path / "f.ttf", "F", "請選案"))
    assert missing_characters(font, ["請選擇要讀取的檔案。", "OK 1", "　"]) == "。取擇檔的要讀"


def test_style_follows_the_family_name(tmp_path: Path, make_font) -> None:
    assert style(TTFont(make_font(tmp_path / "m.ttf", "HGP明朝E", "日"))) == "mincho"
    assert style(TTFont(make_font(tmp_path / "g.ttf", "UmePlus Gothic", "日"))) == "gothic"


def test_script_strings_are_in_script_order(xp_scripts) -> None:
    blob = xp_scripts([("A", 'x = "UmePlus Gothic"\n'), ("B", "y = 'Beta'\n")])
    assert script_strings(blob) == ["UmePlus Gothic", "Beta"]


def test_the_donor_is_found_by_family(tmp_path: Path, make_font, monkeypatch) -> None:
    path = make_font(tmp_path / "donors" / "n.ttf", "Noto Sans TC", "檔")
    monkeypatch.setattr(rgss_font, "donor_dirs", lambda: [tmp_path / "donors"])
    assert find_donor("Noto Sans TC") == (path, 0)
    assert find_donor("GenYoMin2 TW") is None


def _square_font(
    path: Path, upem: int, characters: str, size: int, advance: int, family: str = "Square"
) -> Path:
    """A TrueType face with a ``size`` square for each character, ``advance`` wide."""
    glyphs = [".notdef", *(f"u{ord(c):04X}" for c in characters)]
    outlines = {}
    for glyph in glyphs:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((0, size))
        pen.lineTo((size, size))
        pen.lineTo((size, 0))
        pen.closePath()
        outlines[glyph] = pen.glyph()
    return _save(path, upem, characters, outlines, advance, family)


def _save(path, upem, characters, outlines, advance, family, variations=None) -> Path:
    builder = FontBuilder(upem, isTTF=True)
    builder.setupGlyphOrder(list(outlines))
    builder.setupCharacterMap({ord(c): f"u{ord(c):04X}" for c in characters})
    builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({glyph: (advance, 0) for glyph in outlines})
    builder.setupHorizontalHeader(ascent=upem * 4 // 5, descent=-upem // 5)
    builder.setupNameTable({"familyName": family, "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    if variations is not None:
        builder.setupFvar(axes=[("wght", 100, 400, 900, "Weight")], instances=[])
        builder.setupGvar({glyph: variations for glyph in outlines})
    path.parent.mkdir(parents=True, exist_ok=True)
    builder.save(str(path))
    return path


def _ring(stroke: int):
    """An 800-unit square with a square hole, ``stroke`` thick."""
    pen = TTGlyphPen(None)
    far = 800 - stroke
    for corners in (
        ((0, 0), (0, 800), (800, 800), (800, 0)),
        ((stroke, stroke), (far, stroke), (far, far), (stroke, far)),
    ):
        pen.moveTo(corners[0])
        for corner in corners[1:]:
            pen.lineTo(corner)
        pen.closePath()
    return pen.glyph()


def _ring_font(path: Path, characters: str, stroke: int) -> Path:
    glyphs = [".notdef", *(f"u{ord(c):04X}" for c in characters)]
    return _save(path, 1000, characters, {g: _ring(stroke) for g in glyphs}, 1000, "Ring")


def _variable_ring_font(path: Path, characters: str) -> Path:
    """Rings whose stroke is 100 at wght 400, 200 at 900 and 50 at 100."""

    def inward(by: int) -> list[tuple[int, int]]:
        hole = [(by, by), (-by, by), (-by, -by), (by, -by)]
        return [(0, 0)] * 4 + hole + [(0, 0)] * 4

    glyphs = [".notdef", *(f"u{ord(c):04X}" for c in characters)]
    variations = [
        TupleVariation({"wght": (0, 1.0, 1.0)}, inward(100)),
        TupleVariation({"wght": (-1.0, -1.0, 0)}, inward(-50)),
    ]
    outlines = {g: _ring(100) for g in glyphs}
    return _save(path, 1000, characters, outlines, 1000, "Variable Ring", variations)


def _ink_box(font: TTFont, glyph: str) -> tuple[float, ...]:
    pen = BoundsPen(font.getGlyphSet())
    font.getGlyphSet()[glyph].draw(pen)
    return pen.bounds


def test_the_fill_is_in_the_base_units_and_centred(tmp_path: Path) -> None:
    base = TTFont(_square_font(tmp_path / "b.ttf", 4096, "日本", 3000, 4096))
    donor = TTFont(_square_font(tmp_path / "d.ttf", 1000, "日本檔", 732, 1000))
    data, weight = build_fill(base, donor, "檔")
    fill = TTFont(BytesIO(data))
    assert weight is None and fill["head"].unitsPerEm == 4096
    glyph = fill.getBestCmap()[ord("檔")]
    assert fill["hmtx"][glyph][0] == 4096
    expected = _ink_box(base, base.getBestCmap()[ord("日")])
    assert _ink_box(fill, glyph) == pytest.approx(expected, abs=4)


def test_a_variable_donor_is_pinned_where_its_strokes_match(tmp_path: Path) -> None:
    base = TTFont(_ring_font(tmp_path / "b.ttf", "日本", stroke=140))
    donor = TTFont(_variable_ring_font(tmp_path / "v.ttf", "日本檔"))
    _, weight = build_fill(base, donor, "檔")
    assert weight == 600


@pytest.fixture
def game_and_donor(tmp_path: Path, monkeypatch) -> Path:
    """game/Fonts/ume.ttf maps 日本; the donor Noto Sans TC maps 日本檔."""
    game = tmp_path / "game"
    _square_font(game / "Fonts" / "ume.ttf", 1000, "日本", 800, 1000, "UmePlus Gothic")
    _square_font(tmp_path / "donors" / "n.ttf", 1000, "日本檔", 800, 1000, "Noto Sans TC")
    monkeypatch.setattr(rgss_font, "donor_dirs", lambda: [tmp_path / "donors"])
    return game


def test_the_plan_logs_what_no_font_draws_and_fills_the_rest(game_and_donor: Path, caplog) -> None:
    fill = plan_font_fill(["檔\U00020000"], game_and_donor, None)
    assert fill.characters == "檔" and fill.family == "Bokucho UmePlus Gothic"
    assert fill.file == "Bokucho-UmePlusGothic.ttf" and fill.donor == "Noto Sans TC"
    license = portable_root() / "licenses" / "Noto-Sans-CJK-OFL.txt"
    assert fill.base == "Fonts/ume.ttf" and fill.license == license.read_bytes()
    assert ord("檔") in TTFont(BytesIO(fill.fill)).getBestCmap()
    assert "\U00020000" in caplog.text


def test_no_plan_when_the_base_covers_everything(game_and_donor: Path) -> None:
    assert plan_font_fill(["日本 OK"], game_and_donor, None) is None


def test_no_plan_without_a_donor(game_and_donor: Path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(rgss_font, "donor_dirs", lambda: [])
    assert plan_font_fill(["檔"], game_and_donor, None) is None
    assert "Noto Sans TC" in caplog.text


def test_names_for_the_copy() -> None:
    assert fill_names("UmePlus Gothic") == ("Bokucho UmePlus Gothic", "Bokucho-UmePlusGothic.ttf")
    assert fill_names("あんず") == ("Bokucho Font", "Bokucho-Font.ttf")


def test_a_long_family_is_cut_to_what_windows_finds_a_face_by() -> None:
    family, file = fill_names("Source Han Sans JP Regular Extra")
    assert len(family) <= 31 and family == family.rstrip()
    assert family.startswith("Bokucho Source Han")
    assert file == f"Bokucho-{family[8:].replace(' ', '')}.ttf"


def test_the_longest_family_is_exactly_what_windows_finds_a_face_by() -> None:
    family, file = fill_names("A" * 40)
    assert len(family) == 31
    assert file == f"Bokucho-{'A' * 23}.ttf"
