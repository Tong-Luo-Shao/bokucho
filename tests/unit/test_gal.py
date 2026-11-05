"""Round-trip tests for the GaleX200 writer against pylivemaker's reader."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

pytest.importorskip("livemaker")
import livemaker.GalImagePlugin  # noqa: E402, F401

from bokucho.gal import save_gal  # noqa: E402


def _gradient(mode: str, size: tuple[int, int]) -> Image.Image:
    image = Image.new(mode, size)
    width, height = size
    bands = len(mode)
    image.putdata(
        [
            tuple((x * 31 + y * 47 + band * 71) % 256 for band in range(bands))
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


def test_rgb_roundtrip_with_stride_padding(tmp_path: Path) -> None:
    original = _gradient("RGB", (7, 5))

    path = save_gal(original, tmp_path / "rgb.gal")

    with Image.open(path) as roundtrip:
        roundtrip.load()
        assert roundtrip.size == original.size
        assert roundtrip.convert("RGB").tobytes() == original.tobytes()


def test_rgba_roundtrip_preserves_alpha(tmp_path: Path) -> None:
    original = _gradient("RGBA", (5, 3))

    path = save_gal(original, tmp_path / "rgba.gal")

    with Image.open(path) as roundtrip:
        roundtrip.load()
        assert roundtrip.mode == "RGBA"
        assert roundtrip.tobytes() == original.tobytes()


def _stored_gal(image: Image.Image) -> bytes:
    """``image`` as GraphicsGale saves it uncompressed (``CompType`` 1)."""
    width, height = image.size
    xml = (
        f'<Frames Version="200" Width="{width}" Height="{height}" Bpp="24"'
        ' Count="1" Randomized="0" CompType="1" BlockWidth="0" BlockHeight="0">'
        '<Frame Name="Frame1"><Layers Count="1" Bpp="24">'
        '<Layer Left="0" Top="0" Visible="1" TransColor="-1" Alpha="255"'
        ' AlphaOn="1" Name="Layer1" /></Layers></Frame></Frames>'
    ).encode("shift-jis")
    stride = (width * 3 + 3) & ~3
    bgr = image.convert("RGB").tobytes("raw", "BGR")
    layer = b"".join(
        bgr[y * width * 3 : (y + 1) * width * 3].ljust(stride, b"\0")
        for y in range(height)
    )
    alpha_stride = (width + 3) & ~3
    opacity = image.getchannel("A").tobytes()
    plane = b"".join(
        opacity[y * width : (y + 1) * width].ljust(alpha_stride, b"\0")
        for y in range(height)
    )
    compressed_xml = zlib.compress(xml)
    return b"".join(
        [
            b"GaleX200",
            struct.pack("<I", len(compressed_xml)),
            compressed_xml,
            struct.pack("<i", len(layer)),
            layer,
            struct.pack("<i", len(plane)),
            plane,
        ]
    )


def test_uncompressed_gal_keeps_its_transparency(tmp_path: Path) -> None:
    original = _gradient("RGBA", (5, 3))
    path = tmp_path / "stored.gal"
    path.write_bytes(_stored_gal(original))

    with Image.open(path) as picture:
        picture.load()
        assert picture.mode == "RGBA"
        assert picture.tobytes() == original.tobytes()
