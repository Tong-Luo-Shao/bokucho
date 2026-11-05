"""A binary difference between two versions of one file, made with zstd."""

from __future__ import annotations

import hashlib
import sys
from typing import BinaryIO

if sys.version_info >= (3, 14):
    from compression import zstd
else:
    from backports import zstd

LEVEL = 3
MAX_WINDOW_LOG = 31
CHUNK = 1 << 20


def window_log(*sizes: int) -> int:
    """A window reaching from the end of the larger file back to the start of the prefix."""
    largest = max(sizes)
    needed = max(largest, 1).bit_length() + 1
    if needed > MAX_WINDOW_LOG:
        raise ValueError(f"A file of {largest} bytes is too large to patch.")
    return max(needed, 10)


def make_delta(original: bytes, patched: bytes) -> tuple[bytes, int]:
    """The difference that turns ``original`` into ``patched``, and the window it needs."""
    window = window_log(len(original), len(patched))
    compressor = zstd.ZstdCompressor(
        options={
            zstd.CompressionParameter.compression_level: LEVEL,
            zstd.CompressionParameter.window_log: window,
            zstd.CompressionParameter.enable_long_distance_matching: 1,
        },
        zstd_dict=zstd.ZstdDict(original, is_raw=True).as_prefix,
    )
    return compressor.compress(patched, mode=zstd.ZstdCompressor.FLUSH_FRAME), window


def apply_delta(original: bytes, delta: bytes, window: int, out: BinaryIO) -> str:
    """Write the patched file to ``out`` a chunk at a time and return its SHA-256."""
    decompressor = zstd.ZstdDecompressor(
        zstd_dict=zstd.ZstdDict(original, is_raw=True).as_prefix,
        options={zstd.DecompressionParameter.window_log_max: window},
    )
    digest = hashlib.sha256()
    data = delta
    try:
        while not decompressor.eof:
            chunk = decompressor.decompress(data, max_length=CHUNK)
            data = b""
            if not chunk and decompressor.needs_input:
                raise ValueError("The difference ends before the file does.")
            out.write(chunk)
            digest.update(chunk)
    except zstd.ZstdError as error:
        raise ValueError(f"The difference cannot be decoded: {error}") from error
    return digest.hexdigest()
