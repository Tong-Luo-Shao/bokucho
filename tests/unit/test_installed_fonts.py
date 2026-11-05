"""Fonts on this machine that can typeset a project's pictures."""

from __future__ import annotations

from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

from bokucho.infrastructure import installed_fonts


def test_only_a_face_that_draws_every_character_is_offered(tmp_path: Path, make_font) -> None:
    fonts = tmp_path / "fonts"
    full = make_font(fonts / "full.ttf", "Plate Gothic", "狀態鎮")
    make_font(fonts / "short.ttf", "Plate Mincho", "狀")
    (fonts / "broken.ttf").write_bytes(b"not a font")
    (fonts / "readme.txt").write_text("not a font either", encoding="utf-8")

    found = installed_fonts.fonts_covering("狀態 狀", [fonts])

    assert found == [installed_fonts.InstalledFont(full.as_posix(), "Plate Gothic")]


def test_fonts_in_folders_below_a_font_folder_are_found(tmp_path: Path, make_font) -> None:
    nested = make_font(tmp_path / "opentype" / "noto" / "a.ttf", "Noto Test", "狀")

    assert [font.path for font in installed_fonts.fonts_covering("狀", [tmp_path])] == [
        nested.as_posix()
    ]


def test_a_file_that_is_not_a_font_has_no_name(tmp_path: Path, make_font) -> None:
    (tmp_path / "broken.ttf").write_bytes(b"not a font")

    assert installed_fonts.font_name(tmp_path / "broken.ttf") is None
    assert installed_fonts.font_name(tmp_path / "missing.ttf") is None
    assert (
        installed_fonts.font_name(make_font(tmp_path / "ok.ttf", "Unifont Test", "a", "Medium"))
        == "Unifont Test Medium"
    )


def test_a_shipped_font_is_named_by_its_file(tmp_path: Path, make_font, monkeypatch) -> None:
    monkeypatch.setenv("BOKUCHO_PORTABLE_ROOT", str(tmp_path))
    make_font(tmp_path / "fonts" / "shipped.ttf", "Shipped Gothic", "狀")
    make_font(tmp_path / "fonts" / "extra" / "below.ttf", "Below Gothic", "狀")

    assert installed_fonts.bundled_fonts_covering("狀") == [
        installed_fonts.InstalledFont("bundled:shipped.ttf", "Shipped Gothic")
    ]


def test_changed_text_reuses_font_metadata_and_still_filters_missing_characters(
    tmp_path: Path, make_font, monkeypatch
) -> None:
    full = make_font(tmp_path / "full.ttf", "Full", "狀態")
    short = make_font(tmp_path / "short.ttf", "Short", "狀")
    opened = []
    original_open = installed_fonts._open

    def tracked_open(path):
        font = original_open(path)
        opened.append((path, font.reader.file))
        return font

    monkeypatch.setattr(installed_fonts, "_open", tracked_open)

    assert [font.path for font in installed_fonts.fonts_covering("狀", [tmp_path])] == [
        full.as_posix(),
        short.as_posix(),
    ]
    assert [font.path for font in installed_fonts.fonts_covering("態", [tmp_path])] == [
        full.as_posix()
    ]
    assert installed_fonts.fonts_covering("𠮷", [tmp_path]) == []
    assert [path for path, _ in opened] == [full, short]
    assert all(file.closed for _, file in opened)


def test_coverage_scan_skips_glyph_name_derivation(tmp_path: Path, make_font, monkeypatch) -> None:
    path = make_font(tmp_path / "nameless.ttf", "Nameless", "狀態𠮷")
    with TTFont(path) as font:
        font["post"].formatType = 3.0
        font.save(path)

    def no_glyph_names(self):
        raise AssertionError("Coverage does not need glyph names")

    monkeypatch.setattr(TTFont, "_getGlyphNamesFromCmap", no_glyph_names)

    assert installed_fonts.fonts_covering("狀態𠮷", [tmp_path]) == [
        installed_fonts.InstalledFont(path.as_posix(), "Nameless")
    ]


def test_every_face_offers_its_family_under_each_name_it_goes_by(
    tmp_path: Path, make_font
) -> None:
    make_font(tmp_path / "plain.ttf", "Plate Gothic", "a")
    light = make_font(tmp_path / "light.ttf", "Plate Gothic Light", "a", "Regular")
    with TTFont(light) as font:
        font["name"].setName("Plate Gothic", 16, 3, 1, 0x409)
        font.save(light)
    collection = TTCollection()
    collection.fonts = [
        TTFont(make_font(tmp_path / "parts" / "a.ttf", "Plate Mincho", "a")),
        TTFont(make_font(tmp_path / "parts" / "b.ttf", "Plate Mincho UI", "a")),
    ]
    collection.save(tmp_path / "mincho.ttc")
    (tmp_path / "broken.ttf").write_bytes(b"not a font")

    assert installed_fonts.font_families([tmp_path]) == [
        "Plate Gothic",
        "Plate Gothic Light",
        "Plate Mincho",
        "Plate Mincho UI",
    ]


def test_a_symbol_face_is_not_offered_as_a_family(tmp_path: Path, make_font) -> None:
    make_font(tmp_path / "gothic.ttf", "Plate Gothic", "a")
    dings = make_font(tmp_path / "dings.ttf", "Plate Dings", "a")
    with TTFont(dings) as font:
        # Wingdings' encoding: its letters are pictures.
        for table in font["cmap"].tables:
            if table.platformID == 3:
                table.platEncID = 0
        font.save(dings)

    assert installed_fonts.font_families([tmp_path]) == ["Plate Gothic"]
