"""XP3 archives: the container KiriKiri games ship their files in."""

from __future__ import annotations

import struct

import pytest

from bokucho.engines import xp3


def test_written_files_read_back(tmp_path):
    files = {
        "scenario/first.ks": "おはよう".encode("utf-16"),
        "名前.txt": b"",
        "b.bin": bytes(range(256)) * 9,
    }
    xp3.write(tmp_path / "a.xp3", files)
    found = {e.name: xp3.read(tmp_path / "a.xp3", e) for e in xp3.entries(tmp_path / "a.xp3")}
    assert found == files


def test_raw_and_zlib_segments_of_one_file_join(tmp_path, xp3_bytes):
    path = tmp_path / "a.xp3"
    path.write_bytes(xp3_bytes([("x.ks", [(False, b"abc"), (True, b"def")])]))
    (entry,) = xp3.entries(path)
    assert xp3.read(path, entry) == b"abcdef"
    assert entry.size == 6


def test_backslashes_in_names_become_slashes(tmp_path, xp3_bytes):
    path = tmp_path / "a.xp3"
    path.write_bytes(xp3_bytes([("scenario\\x.ks", [(True, b"t")])]))
    assert [e.name for e in xp3.entries(path)] == ["scenario/x.ks"]


def test_the_newer_header_is_read(tmp_path, xp3_bytes):
    path = tmp_path / "a.xp3"
    path.write_bytes(xp3_bytes([("x.ks", [(True, b"text")])], cushion=True))
    (entry,) = xp3.entries(path)
    assert entry.name == "x.ks"
    assert xp3.read(path, entry) == b"text"


def _damaged(blob: bytes, damage: str) -> bytes:
    (index_at,) = struct.unpack_from("<Q", blob, 11)
    if damage == "magic":
        return b"Y" + blob[1:]
    if damage == "method":
        return blob[:index_at] + b"\x07" + blob[index_at + 1 :]
    if damage == "truncated":
        return blob[: index_at + (len(blob) - index_at) // 2]
    out = bytearray(blob)
    if damage == "offset":
        segm = out.index(b"segm")
        struct.pack_into("<Q", out, segm + 12 + 4, len(blob) + 10)
    else:  # oversized
        info = out.index(b"info")
        struct.pack_into("<Q", out, info + 12 + 4, xp3.MAX_SIZE + 1)
    return bytes(out)


@pytest.mark.parametrize("damage", ["magic", "method", "truncated", "offset", "oversized"])
def test_a_damaged_archive_is_refused(tmp_path, xp3_bytes, damage):
    path = tmp_path / "a.xp3"
    path.write_bytes(_damaged(xp3_bytes([("x.ks", [(True, b"text")])]), damage))
    with pytest.raises(xp3.NotAnArchive):
        for entry in xp3.entries(path):
            xp3.read(path, entry)


def test_a_publisher_index_chunk_is_reported(tmp_path, xp3_bytes):
    path = tmp_path / "a.xp3"
    path.write_bytes(xp3_bytes([("x.ks", [(True, b"t")])], extra=[(b"Hxv4", b"\0" * 8)]))
    with pytest.raises(xp3.CustomIndex) as caught:
        xp3.entries(path)
    assert caught.value.tags == ("Hxv4",)
