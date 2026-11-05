"""Where Bokucho keeps what it carries."""

from __future__ import annotations

from pathlib import Path

from bokucho import appdirs


def test_a_shipped_font_is_found_beside_the_program(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOKUCHO_PORTABLE_ROOT", str(tmp_path))

    expected = (tmp_path / "fonts" / "Gothic.otf").as_posix()

    assert appdirs.locate_font("bundled:Gothic.otf") == expected


def test_any_other_font_is_left_as_it_was_named() -> None:
    assert appdirs.locate_font("C:/Windows/Fonts/msjh.ttc") == "C:/Windows/Fonts/msjh.ttc"
    assert appdirs.locate_font("fonts/own.ttf") == "fonts/own.ttf"
    assert appdirs.locate_font(None) is None


def test_a_shipped_name_cannot_reach_out_of_the_fonts_folder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOKUCHO_PORTABLE_ROOT", str(tmp_path))

    for ref in ("bundled:../Resource/credentials.json", "bundled:sub/x.ttf", "bundled:"):
        assert appdirs.locate_font(ref) == ref
