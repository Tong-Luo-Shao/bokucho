"""LivePixelFont (``.lft``) reader and writer."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"LivePixelFont\x00"
HEADER_END = 0x142
NAME_AT = 0x1A
NAME_SIZE = 64
RECORD = 18
RECORD_FORMAT = "<HHHHHHHI"


def stride(width: int) -> int:
    """Row pitch in bytes -- rows are padded out like a Windows DIB."""
    return (width + 3) & ~3


def flip(pixels: bytes, width: int) -> bytes:
    """Swap between the file's bottom-up rows and top-down ones."""
    pitch = stride(width)
    return b"".join(
        pixels[start : start + pitch] for start in range(len(pixels) - pitch, -1, -pitch)
    )


@dataclass
class Glyph:
    code: int
    width: int
    height: int
    bearing_x: int
    bearing_y: int
    size: int
    reserved: int
    offset: int
    pixels: bytes

    @property
    def char(self) -> str:
        """The character this glyph draws, or "" if the code is not CP932."""
        raw = (
            bytes((self.code >> 8, self.code & 0xFF))
            if self.code > 0xFF
            else bytes((self.code,))
        )
        try:
            return raw.decode("cp932")
        except UnicodeDecodeError:
            return ""


class PixelFont:
    """A parsed ``.lft``. Mutate ``glyphs`` in place, then ``to_bytes()``."""

    def __init__(self, path: str | Path) -> None:
        raw = Path(path).read_bytes()
        if not raw.startswith(MAGIC):
            raise ValueError(f"{path}: not a LivePixelFont file")
        self.version, index_size, blob_size = struct.unpack_from("<III", raw, 0x0E)
        self.name = raw[NAME_AT : NAME_AT + NAME_SIZE].split(b"\x00")[0].decode("cp932")
        self.descriptor = raw[NAME_AT + NAME_SIZE : HEADER_END]
        index = zlib.decompress(raw[HEADER_END : HEADER_END + index_size])
        blob = raw[HEADER_END + index_size :]
        if len(blob) != blob_size:
            raise ValueError(f"{path}: blob is {len(blob)} bytes, header says {blob_size}")
        self.glyphs = [
            Glyph(
                *fields,
                flip(blob[fields[7] : fields[7] + stride(fields[1]) * fields[2]], fields[1]),
            )
            for fields in (
                struct.unpack_from(RECORD_FORMAT, index, offset)
                for offset in range(0, len(index), RECORD)
            )
        ]
        self.by_code = {glyph.code: glyph for glyph in self.glyphs}

    def glyph(self, ch: str) -> Glyph | None:
        """The glyph for ``ch``, or None when the font has no code for it."""
        try:
            code = int.from_bytes(ch.encode("cp932"), "big")
        except UnicodeEncodeError:
            return None
        return self.by_code.get(code)

    def to_bytes(self) -> bytes:
        """Re-emit the font, relaying the blob so glyphs may change size."""
        index = bytearray()
        blob = bytearray()
        for glyph in self.glyphs:
            expected = stride(glyph.width) * glyph.height
            if len(glyph.pixels) != expected:
                raise ValueError(
                    f"glyph {glyph.code:#06x}: {len(glyph.pixels)} bytes of pixels, "
                    f"{glyph.width}x{glyph.height} needs {expected}"
                )
            index += struct.pack(
                RECORD_FORMAT,
                glyph.code,
                glyph.width,
                glyph.height,
                glyph.bearing_x,
                glyph.bearing_y,
                glyph.size,
                glyph.reserved,
                len(blob),
            )
            blob += flip(glyph.pixels, glyph.width)
        compressed = zlib.compress(bytes(index), 9)
        header = bytearray(MAGIC)
        header += struct.pack("<III", self.version, len(compressed), len(blob))
        header += self.name.encode("cp932").ljust(NAME_SIZE, b"\x00")
        header += self.descriptor
        return bytes(header) + compressed + bytes(blob)
