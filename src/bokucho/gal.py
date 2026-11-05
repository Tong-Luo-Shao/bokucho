"""Minimal GraphicsGale (GAL/X200) image writer."""

from __future__ import annotations

import struct
import zlib
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

from PIL import Image

try:
    from livemaker.GalImagePlugin import _GAL_COMPRESSION, GalImageDecoder
except ImportError:  # pragma: no cover - exercised only without the extra
    GalImageDecoder = None
    _GAL_COMPRESSION = {}

if GalImageDecoder is not None:

    class _GalDecoder(GalImageDecoder):
        """pylivemaker's decoder, reading an uncompressed layer's alpha too."""

        def decode(self, buffer):
            result = super().decode(buffer)
            info, _, _, frame_index = self.args
            frame = info["frames"][frame_index]
            if (
                info["compression"] not in _GAL_COMPRESSION
                and frame["layers"][0]["alpha_on"]
                and "A" in self.mode
            ):
                size = struct.unpack("<i", self.fd.read(4))[0]
                plane = self._unpack_layer(
                    BytesIO(self.fd.read(size)),
                    frame,
                    info["block_width"],
                    info["block_height"],
                    info["randomized"],
                    info["frames"],
                    True,
                )
                mask = Image.frombytes(
                    "L",
                    (self.state.xsize, self.state.ysize),
                    plane,
                    "raw",
                    "L",
                    frame["alpha_stride"],
                )
                self.im.putband(mask.im, len(self.mode) - 1)
            return result

    Image.register_decoder("GAL", _GalDecoder)

_XML_TEMPLATE = (
    '<Frames Version="200" Width={width} Height={height} Bpp="24" Count="1"'
    ' SyncPal="1" Randomized="0" CompType="0" CompLevel="6"'
    ' BGColor="16777215" BlockWidth="0" BlockHeight="0" NotFillBG="0">\r\n'
    ' <Frame Name="Frame1" TransColor="-1" Delay="17" Disposal="2">\r\n'
    "  <Layers Count=\"1\" Width={width} Height={height} Bpp=\"24\">\r\n"
    '   <Layer Left="0" Top="0" Visible="1" TransColor="-1" Alpha="255"'
    ' AlphaOn={alpha_on} Name="Layer1" Lock="0" />\r\n'
    "  </Layers>\r\n"
    " </Frame>\r\n"
    "</Frames>"
)


def frame_count(path: str | Path) -> int:
    """How many frames the container at ``path`` declares."""
    try:
        with Path(path).open("rb") as handle:
            magic = handle.read(8)
            if magic == b"GaleX200":
                length = struct.unpack("<I", handle.read(4))[0]
                xml = zlib.decompress(handle.read(length)).decode("shift-jis")
                return max(1, int(ElementTree.fromstring(xml).get("Count", 1)))
            if not magic.startswith(b"Gale"):
                return 1
            if int(magic[4:7]) <= 102:
                return 1
            handle.seek(7)
            header = handle.read(struct.unpack("<i", handle.read(4))[0])
            return max(1, struct.unpack_from("<i", header, 0x10)[0])
    except (OSError, ValueError, struct.error, zlib.error, ElementTree.ParseError):
        return 1


def _padded_rows(data: bytes, row_bytes: int, stride: int, height: int) -> bytes:
    if row_bytes == stride:
        return data
    padding = b"\x00" * (stride - row_bytes)
    return b"".join(
        data[y * row_bytes : (y + 1) * row_bytes] + padding for y in range(height)
    )


def save_gal(image: Image.Image, path: str | Path) -> Path:
    """Write ``image`` as a single-frame GaleX200 file and return its path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_gal(image))
    return path


def dump_gal(image: Image.Image) -> bytes:
    """Serialize ``image`` as a single-frame GaleX200 container."""
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    rgba = image.convert("RGBA") if has_alpha else image.convert("RGB")
    width, height = rgba.size

    xml = _XML_TEMPLATE.format(
        width=quoteattr(str(width)),
        height=quoteattr(str(height)),
        alpha_on=quoteattr("1" if has_alpha else "0"),
    ).encode("shift-jis")

    stride = (width * 3 + 3) & ~3
    bgr = rgba.convert("RGB").tobytes("raw", "BGR")
    layer = zlib.compress(_padded_rows(bgr, width * 3, stride, height))

    compressed_xml = zlib.compress(xml)
    chunks = [b"GaleX200", struct.pack("<I", len(compressed_xml)), compressed_xml]
    chunks.append(struct.pack("<i", len(layer)))
    chunks.append(layer)
    if has_alpha:
        alpha_stride = (width + 3) & ~3
        alpha = rgba.getchannel("A").tobytes("raw", "L")
        alpha_plane = zlib.compress(
            _padded_rows(alpha, width, alpha_stride, height)
        )
        chunks.append(struct.pack("<i", len(alpha_plane)))
        chunks.append(alpha_plane)

    return b"".join(chunks)
