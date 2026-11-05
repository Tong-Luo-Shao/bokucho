"""The LivePixelFont container, read and written back."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from bokucho.engines import livepixelfont as lft


def _build(path: Path, name: str, glyphs: list[tuple[str, int, int, bytes]]) -> None:
    """Write a .lft holding ``(char, width, height, pixels)`` in code order."""
    index = bytearray()
    blob = bytearray()
    for char, width, height, pixels in glyphs:
        code = int.from_bytes(char.encode("cp932"), "big")
        index += struct.pack(
            lft.RECORD_FORMAT, code, width, height, 1, 17, 20, 0, len(blob)
        )
        blob += pixels
    compressed = zlib.compress(bytes(index), 9)
    header = bytearray(lft.MAGIC)
    header += struct.pack("<III", 100, len(compressed), len(blob))
    header += name.encode("cp932").ljust(lft.NAME_SIZE, b"\x00")
    descriptor = bytearray(lft.HEADER_END - lft.NAME_AT - lft.NAME_SIZE)
    struct.pack_into("<I", descriptor, 12, len(glyphs))
    header += descriptor
    path.write_bytes(bytes(header) + compressed + bytes(blob))


def _cell(width: int, height: int, fill: int) -> bytes:
    return bytes([fill]) * (lft.stride(width) * height)


def test_rows_are_padded_to_four_bytes() -> None:
    assert [lft.stride(w) for w in (1, 3, 4, 5, 17, 19, 20)] == [4, 4, 4, 8, 20, 20, 20]


def test_the_last_row_in_the_file_is_the_top_of_the_glyph(tmp_path: Path) -> None:
    """Rows are stored bottom-up."""
    width, height = 5, 3
    pitch = lft.stride(width)
    stored = bytes(pitch * (height - 1)) + b"\xff" * pitch
    path = tmp_path / "font0.lft"
    _build(path, "font", [("漢", width, height, stored)])

    font = lft.PixelFont(path)

    pixels = font.glyphs[0].pixels
    assert pixels[:pitch] == b"\xff" * pitch
    assert pixels[pitch:] == bytes(pitch * (height - 1))
    assert font.to_bytes()[-len(stored) :] == stored


def test_a_font_written_by_hand_reads_back_whole(tmp_path: Path) -> None:
    path = tmp_path / "font0.lft"
    _build(
        path,
        "あずきフォントP",
        [("A", 10, 18, _cell(10, 18, 0x40)), ("漢", 17, 19, _cell(17, 19, 0x80))],
    )

    font = lft.PixelFont(path)

    assert font.name == "あずきフォントP"
    assert [glyph.char for glyph in font.glyphs] == ["A", "漢"]
    assert font.glyph("漢").pixels == _cell(17, 19, 0x80)
    assert font.glyph("永") is None
    assert font.glyph("你") is None


def test_re_emitting_an_untouched_font_changes_no_glyph(tmp_path: Path) -> None:
    path = tmp_path / "font0.lft"
    _build(path, "font", [("A", 10, 18, _cell(10, 18, 1)), ("漢", 17, 19, _cell(17, 19, 2))])

    once = lft.PixelFont(path)
    rewritten = tmp_path / "again.lft"
    rewritten.write_bytes(once.to_bytes())
    twice = lft.PixelFont(rewritten)

    assert [
        (g.code, g.width, g.height, g.bearing_x, g.bearing_y, g.size, g.pixels)
        for g in twice.glyphs
    ] == [
        (g.code, g.width, g.height, g.bearing_x, g.bearing_y, g.size, g.pixels)
        for g in once.glyphs
    ]


def test_a_glyph_that_grows_pushes_the_rest_along(tmp_path: Path) -> None:
    """Offsets are rebuilt on write, so a donor may be given a bigger cell."""
    path = tmp_path / "font0.lft"
    _build(path, "font", [("A", 4, 4, _cell(4, 4, 1)), ("漢", 17, 19, _cell(17, 19, 2))])

    font = lft.PixelFont(path)
    font.glyphs[0].width = 17
    font.glyphs[0].height = 19
    font.glyphs[0].pixels = _cell(17, 19, 3)
    grown = tmp_path / "grown.lft"
    grown.write_bytes(font.to_bytes())

    after = lft.PixelFont(grown)
    assert after.glyph("A").pixels == _cell(17, 19, 3)
    assert after.glyph("漢").pixels == _cell(17, 19, 2)
    assert after.glyph("漢").offset == lft.stride(17) * 19


def test_a_truncated_blob_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "font0.lft"
    _build(path, "font", [("漢", 17, 19, _cell(17, 19, 2))])
    path.write_bytes(path.read_bytes()[:-4])

    with pytest.raises(ValueError, match="blob"):
        lft.PixelFont(path)


def test_a_file_that_is_not_a_pixel_font_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "not.lft"
    path.write_bytes(b"MZ\x90\x00" + bytes(64))

    with pytest.raises(ValueError, match="not a LivePixelFont"):
        lft.PixelFont(path)
