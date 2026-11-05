from __future__ import annotations

import struct
from pathlib import Path

import pytest

from bokucho.engines.base import NoPixelFont
from bokucho.engines.livemaker import (
    PROJECT_SETTINGS,
    LiveMakerAdapter,
    replace_status_font,
    status_font_name,
)

DEFAULT = "ＭＳ ゴシック".encode("cp932")


def _setting(name: bytes, kind: int, value: bytes) -> bytes:
    return (
        bytes([kind]) + struct.pack("<I", len(name)) + name + struct.pack("<I", len(value)) + value
    )


def _lpb(default: bytes = DEFAULT) -> bytes:
    """The shape live.lpb has around the setting: name, length, CP932 value, the next setting."""
    return (
        b"LPB-HEADER\x01\x02"
        + _setting(b"StatusTextSpeed", 2, b"\x80\x00\x00\x00")
        + _setting(b"StatusFontName", 4, default)
        + _setting(b"StatusSkipRead", 3, b"\x00")
        + b"the rest of the file, which nothing here understands"
    )


def test_the_default_face_is_read() -> None:
    assert status_font_name(_lpb()) == DEFAULT


def test_the_default_is_replaced_in_place_and_nothing_else_moves() -> None:
    original = _lpb()
    replaced = replace_status_font(original, "Bokucho-a1b2c")
    assert len(replaced) == len(original)
    assert status_font_name(replaced) == b"Bokucho-a1b2c"
    start = original.index(DEFAULT)
    assert replaced[:start] == original[:start]
    assert replaced[start + len(DEFAULT) :] == original[start + len(DEFAULT) :]


def test_a_family_of_another_length_is_refused() -> None:
    with pytest.raises(ValueError, match="13"):
        replace_status_font(_lpb(), "Bokucho-a1b2cd")


def test_a_file_without_the_setting_is_refused() -> None:
    with pytest.raises(ValueError, match="StatusFontName"):
        status_font_name(b"LPB-HEADER" + _setting(b"StatusSkipRead", 3, b"\x00"))


@pytest.mark.parametrize("tail", [b"", b"\x0d", b"\x0d\x00", b"\x0d\x00\x00"])
def test_a_file_cut_short_inside_the_setting_is_refused(tail: bytes) -> None:
    lpb = b"LPB" + struct.pack("<I", len(b"StatusFontName")) + b"StatusFontName" + tail
    with pytest.raises(ValueError, match="past the end"):
        status_font_name(lpb)


def test_the_project_settings_are_read_from_the_game(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    (game / PROJECT_SETTINGS).write_bytes(_lpb())
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    assert adapter.project_settings(game) == _lpb()


def test_a_game_without_a_pixel_font_says_so_in_its_own_kind(tmp_path: Path) -> None:
    game = tmp_path / "game"
    game.mkdir()
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    with pytest.raises(NoPixelFont):
        adapter.pixel_font(game)
    assert issubclass(NoPixelFont, FileNotFoundError)
