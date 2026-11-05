"""RGSSAD v1 (.rgssad, .rgss2a) and v3 (.rgss3a) — the containers XP, VX and VX Ace ship data in."""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"RGSSAD\0"
VERSION1 = 1
VERSION3 = 3
_MASK = 0xFFFFFFFF
_V1_SEED = 0xDEADCAFE


class NotAnArchive(RuntimeError):
    """The file is not an RGSSAD v1 or v3 archive."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One file inside an archive: where it is, how long, and its own key."""

    name: str
    offset: int
    size: int
    key: int

    @property
    def path(self) -> str:
        """The name as a relative POSIX path; the archive writes Windows separators."""
        return self.name.replace("\\", "/")


def _advance(key: int) -> int:
    return (key * 7 + 3) & _MASK


def _keystream(key: int, length: int) -> bytes:
    out = bytearray()
    current = key
    while len(out) < length:
        out += struct.pack("<I", current)
        current = _advance(current)
    return bytes(out[:length])


def _table(blob: bytes, label: str) -> tuple[int, list[tuple[Entry, int, int]]]:
    """The archive's version, and every entry with where its record starts and that record's key."""
    message = f"{label} is not an RGSSAD archive"
    header = len(MAGIC) + 1
    if len(blob) < header or blob[: len(MAGIC)] != MAGIC:
        raise NotAnArchive(message)
    version = blob[len(MAGIC)]
    if version == VERSION1:
        return version, _table_v1(blob, message)
    if version == VERSION3:
        return version, _table_v3(blob, message)
    raise NotAnArchive(message)


def _table_v1(blob: bytes, message: str) -> list[tuple[Entry, int, int]]:
    """Walk the v1 stream; a record is the size field, and its key is the running key there."""
    key = _V1_SEED
    cursor = len(MAGIC) + 1
    found: list[tuple[Entry, int, int]] = []
    while cursor < len(blob):
        try:
            name_len = struct.unpack_from("<I", blob, cursor)[0] ^ key
        except struct.error as error:
            raise NotAnArchive(message) from error
        key = _advance(key)
        cursor += 4
        if cursor + name_len > len(blob):
            raise NotAnArchive(message)
        name = bytearray()
        for byte in blob[cursor : cursor + name_len]:
            name.append(byte ^ (key & 0xFF))
            key = _advance(key)
        cursor += name_len
        record, size_key = cursor, key
        try:
            size = struct.unpack_from("<I", blob, cursor)[0] ^ key
            text = name.decode("utf-8")
        except (struct.error, UnicodeDecodeError) as error:
            raise NotAnArchive(message) from error
        key = _advance(key)
        found.append((Entry(text, cursor + 4, size, key), record, size_key))
        cursor += 4 + size
    return found


def _table_v3(blob: bytes, message: str) -> list[tuple[Entry, int, int]]:
    """Read the v3 table; every record is encrypted with the table key."""
    header = len(MAGIC) + 1
    try:
        seed = struct.unpack_from("<I", blob, header)[0]
    except struct.error as error:
        raise NotAnArchive(message) from error
    key = (seed * 9 + 3) & _MASK
    key_bytes = struct.pack("<I", key)
    found: list[tuple[Entry, int, int]] = []
    cursor = header + 4
    while True:
        record = cursor
        try:
            offset, size, file_key, name_len = (
                value ^ key for value in struct.unpack_from("<IIII", blob, cursor)
            )
        except struct.error as error:
            raise NotAnArchive(message) from error
        cursor += 16
        if offset == 0:
            return found
        if cursor + name_len > len(blob):
            raise NotAnArchive(message)
        raw = blob[cursor : cursor + name_len]
        cursor += name_len
        name = bytes(byte ^ key_bytes[i % 4] for i, byte in enumerate(raw))
        try:
            found.append((Entry(name.decode("utf-8"), offset, size, file_key), record, key))
        except UnicodeDecodeError as error:
            raise NotAnArchive(message) from error


def entries(path: Path) -> list[Entry]:
    """Every file the archive at ``path`` holds."""
    target = Path(path)
    return [entry for entry, _, _ in _table(target.read_bytes(), target.name)[1]]


class EntryUnreadable(RuntimeError):
    """One file inside the archive cannot be read, though the archive can."""


def _crypt(data: bytes, key: int) -> bytes:
    """``data`` XOR'd with ``key``'s keystream: encrypting and decrypting are one step."""
    stream = _keystream(key, len(data))
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(
        len(data), "little"
    )


def read(path: Path, entry: Entry) -> bytes:
    """``entry``'s decrypted content."""
    with Path(path).open("rb") as handle:
        handle.seek(entry.offset)
        chunk = handle.read(entry.size)
    if len(chunk) != entry.size:
        raise EntryUnreadable(
            f"{entry.name} claims {entry.size} bytes and the archive holds "
            f"{len(chunk)} from where it says they start"
        )
    return _crypt(chunk, entry.key)


def _splice_v1(
    view: memoryview, table: list[tuple[Entry, int, int]], wanted: Mapping[str, bytes]
) -> list[bytes | memoryview]:
    """The stream with each replaced entry's size field and data swapped; every other byte stays."""
    pieces: list[bytes | memoryview] = []
    cursor = 0
    for entry, record, size_key in table:
        replacement = wanted.get(entry.path.casefold())
        if replacement is None:
            continue
        pieces += [
            view[cursor:record],
            struct.pack("<I", len(replacement) ^ size_key),
            _crypt(replacement, entry.key),
        ]
        cursor = entry.offset + entry.size
    pieces.append(view[cursor:])
    return pieces


def _splice_v3(
    view: memoryview, table: list[tuple[Entry, int, int]], wanted: Mapping[str, bytes]
) -> list[bytes | memoryview]:
    """The header with every offset and size rewritten, then the data repacked back to back."""
    start = min((entry.offset for entry, _, _ in table), default=len(view))
    header = bytearray(view[:start])
    pieces: list[bytes | memoryview] = [header]
    cursor = start
    for entry, record, key in sorted(table, key=lambda item: item[0].offset):
        replacement = wanted.get(entry.path.casefold())
        if replacement is None:
            piece: bytes | memoryview = view[entry.offset : entry.offset + entry.size]
        else:
            piece = _crypt(replacement, entry.key)
        struct.pack_into("<II", header, record, cursor ^ key, len(piece) ^ key)
        pieces.append(piece)
        cursor += len(piece)
    return pieces


def rebuild(path: Path, replacements: Mapping[str, bytes], out: Path) -> None:
    """Write ``path`` to ``out`` with the named entries' content replaced."""
    source = Path(path)
    blob = source.read_bytes()
    version, table = _table(blob, source.name)
    by_name = {entry.path.casefold(): entry for entry, _, _ in table}
    wanted = {name.casefold(): data for name, data in replacements.items()}
    missing = sorted(name for name in replacements if name.casefold() not in by_name)
    if missing:
        raise ValueError(f"{source.name} holds no " + ", ".join(missing))
    for entry, _, _ in table:
        if entry.offset + entry.size > len(blob):
            raise EntryUnreadable(f"{entry.name} runs past the end of {source.name}")
    splice = _splice_v1 if version == VERSION1 else _splice_v3
    pieces = splice(memoryview(blob), table, wanted)
    with Path(out).open("xb") as stream:
        for piece in pieces:
            stream.write(piece)
