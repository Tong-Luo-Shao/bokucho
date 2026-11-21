"""XP3 — the archive KiriKiri games ship their files in (plain, unencrypted archives only)."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAGIC = b"XP3\r\n \n\x1a\x8b\x67\x01"
# The largest index, and the largest single file, this reader will hold in memory.
MAX_SIZE = 256 << 20
_MAX_BLOCKS = 16
_METHOD = 0x07
_CONTINUE = 0x80
_RAW = 0
_ZLIB = 1
_PROTECTED = 1 << 31
_CHUNK = struct.Struct("<4sQ")
_INFO = struct.Struct("<IQQH")
_SEGMENT = struct.Struct("<IQQQ")


class NotAnArchive(RuntimeError):
    """The file is not a plain XP3 archive this reader understands."""


class CustomIndex(NotAnArchive):
    """The index holds chunks of a publisher's own format besides ``File``."""

    def __init__(self, path: Path, tags: tuple[str, ...]) -> None:
        super().__init__(
            f"{path}: the archive index has chunks of its own format: {', '.join(tags)}."
        )
        self.tags = tags


@dataclass(frozen=True, slots=True)
class Segment:
    """One stretch of a file's bytes in the archive, raw or zlib-compressed."""

    compressed: bool
    offset: int
    size: int
    stored: int


@dataclass(frozen=True, slots=True)
class Entry:
    """One file in an archive; ``name`` uses ``/`` whatever the archive wrote."""

    name: str
    size: int
    protected: bool
    segments: tuple[Segment, ...]


def _exact(f: BinaryIO, size: int, path: Path) -> bytes:
    if size > MAX_SIZE:
        raise NotAnArchive(f"{path}: a block is larger than {MAX_SIZE >> 20} MiB.")
    data = f.read(size)
    if len(data) != size:
        raise NotAnArchive(f"{path}: the archive is cut short.")
    return data


def _inflate(data: bytes, size: int, path: Path) -> bytes:
    if size > MAX_SIZE:
        raise NotAnArchive(f"{path}: a block is larger than {MAX_SIZE >> 20} MiB.")
    inflater = zlib.decompressobj()
    try:
        out = inflater.decompress(data, size + 1)
    except zlib.error as error:
        raise NotAnArchive(f"{path}: a compressed block is damaged.") from error
    if len(out) != size or not inflater.eof:
        raise NotAnArchive(f"{path}: a compressed block does not match its recorded size.")
    return out


def _index(f: BinaryIO, path: Path) -> bytes:
    head = f.read(len(MAGIC) + 8)
    if len(head) != len(MAGIC) + 8 or not head.startswith(MAGIC):
        raise NotAnArchive(f"{path}: not an XP3 archive.")
    (at,) = struct.unpack_from("<Q", head, len(MAGIC))
    data = b""
    for _ in range(_MAX_BLOCKS):
        f.seek(at)
        flag = _exact(f, 1, path)[0]
        method = flag & _METHOD
        if method == _ZLIB:
            stored, size = struct.unpack("<QQ", _exact(f, 16, path))
            data = _inflate(_exact(f, stored, path), size, path)
        elif method == _RAW:
            (size,) = struct.unpack("<Q", _exact(f, 8, path))
            # The 2.28+ header's cushion is an empty raw block that only points onward.
            data = _exact(f, size, path) if size else data
        else:
            raise NotAnArchive(f"{path}: the archive index uses an unknown method ({method}).")
        if not flag & _CONTINUE:
            return data
        (at,) = struct.unpack("<Q", _exact(f, 8, path))
    raise NotAnArchive(f"{path}: the archive index never ends.")


def _chunks(data: bytes, path: Path) -> Iterator[tuple[bytes, bytes]]:
    at = 0
    while at < len(data):
        if at + _CHUNK.size > len(data):
            raise NotAnArchive(f"{path}: the archive index is damaged.")
        tag, size = _CHUNK.unpack_from(data, at)
        end = at + _CHUNK.size + size
        if end > len(data):
            raise NotAnArchive(f"{path}: the archive index is damaged.")
        yield tag, data[at + _CHUNK.size : end]
        at = end


def _entry(body: bytes, path: Path) -> Entry:
    info = segm = None
    for tag, sub in _chunks(body, path):
        if tag == b"info":
            info = sub
        elif tag == b"segm":
            segm = sub
    if info is None or segm is None or len(info) < _INFO.size or len(segm) % _SEGMENT.size:
        raise NotAnArchive(f"{path}: a file in the archive index is damaged.")
    flags, size, _stored, length = _INFO.unpack_from(info)
    raw_name = info[_INFO.size : _INFO.size + length * 2]
    try:
        name = raw_name.decode("utf-16-le")
    except UnicodeDecodeError as error:
        raise NotAnArchive(f"{path}: a file name in the archive index is damaged.") from error
    if len(raw_name) != length * 2:
        raise NotAnArchive(f"{path}: a file name in the archive index is damaged.")
    segments = []
    for at in range(0, len(segm), _SEGMENT.size):
        seg_flags, offset, seg_size, stored = _SEGMENT.unpack_from(segm, at)
        if seg_flags & _METHOD not in (_RAW, _ZLIB):
            raise NotAnArchive(f"{path}: {name} uses an unknown compression.")
        segments.append(Segment(seg_flags & _METHOD == _ZLIB, offset, seg_size, stored))
    return Entry(name.replace("\\", "/"), size, bool(flags & _PROTECTED), tuple(segments))


def entries(path: Path) -> list[Entry]:
    """Every file the archive lists, in index order."""
    with path.open("rb") as f:
        index = _index(f, path)
    found: list[Entry] = []
    other: list[str] = []
    for tag, body in _chunks(index, path):
        if tag == b"File":
            found.append(_entry(body, path))
        else:
            other.append(tag.decode("latin-1"))
    if other:
        raise CustomIndex(path, tuple(dict.fromkeys(other)))
    return found


def read(path: Path, entry: Entry) -> bytes:
    """The contents of one file, its segments joined."""
    if entry.size > MAX_SIZE:
        raise NotAnArchive(f"{path}: {entry.name} is larger than {MAX_SIZE >> 20} MiB.")
    out = bytearray()
    with path.open("rb") as f:
        for segment in entry.segments:
            if segment.size > entry.size - len(out):
                raise NotAnArchive(f"{path}: {entry.name} does not match its recorded size.")
            f.seek(segment.offset)
            data = _exact(f, segment.stored, path)
            if segment.compressed:
                data = _inflate(data, segment.size, path)
            elif len(data) != segment.size:
                raise NotAnArchive(f"{path}: {entry.name} does not match its recorded size.")
            out += data
    if len(out) != entry.size:
        raise NotAnArchive(f"{path}: {entry.name} does not match its recorded size.")
    return bytes(out)


def _chunk(tag: bytes, body: bytes) -> bytes:
    return _CHUNK.pack(tag, len(body)) + body


def write(path: Path, files: Mapping[str, bytes]) -> None:
    """Write a plain archive (pre-2.28 header, one zlib segment per file, zlib index)."""
    head = len(MAGIC) + 8
    body = bytearray()
    index = bytearray()
    for name in sorted(files):
        raw = files[name]
        packed = zlib.compress(raw)
        encoded = name.encode("utf-16-le")
        info = _INFO.pack(0, len(raw), len(packed), len(encoded) // 2) + encoded
        segm = _SEGMENT.pack(_ZLIB, head + len(body), len(raw), len(packed))
        adlr = struct.pack("<I", zlib.adler32(raw))
        index += _chunk(
            b"File", _chunk(b"info", info) + _chunk(b"segm", segm) + _chunk(b"adlr", adlr)
        )
        body += packed
    packed_index = zlib.compress(bytes(index))
    path.write_bytes(
        MAGIC
        + struct.pack("<Q", head + len(body))
        + bytes(body)
        + bytes([_ZLIB])
        + struct.pack("<QQ", len(packed_index), len(index))
        + packed_index
    )
