"""Tests for reading the string literals of an IL2CPP Unity build."""

import struct

import pytest

from bokucho.engines.unity_il2cpp import METADATA, metadata_string_literals

SAMPLES = ["はい", "", "Start", "長" * 100, "改行\nあり"]


def metadata(literals: list[str], version: int = 31, *, sanity: int = 0xFAB11BAF) -> bytes:
    """A global-metadata.dat holding only a string literal table of ``literals``."""
    encoded = [text.encode("utf-8") for text in literals]
    starts = [sum(len(item) for item in encoded[:index]) for index in range(len(encoded))]
    if version < 35:
        table = b"".join(
            struct.pack("<II", len(item), start)
            for item, start in zip(encoded, starts, strict=True)
        )
    else:
        table = b"".join(struct.pack("<I", start) for start in starts)
    data = b"".join(encoded)
    header_size = 256
    sections = [(header_size, len(table), len(literals)), (header_size + len(table), len(data), 0)]
    header = struct.pack("<Ii", sanity, version)
    for offset, size, count in sections:
        header += struct.pack("<iii", offset, size, count) if version >= 38 else struct.pack(
            "<ii", offset, size
        )
    return header.ljust(header_size, b"\0") + table + data


def test_metadata_lives_under_the_data_folder():
    assert METADATA == "il2cpp_data/Metadata/global-metadata.dat"


@pytest.mark.parametrize("version", [24, 27, 29, 31])
def test_reads_every_literal_in_order_with_stored_lengths(version):
    assert metadata_string_literals(metadata(SAMPLES, version)) == SAMPLES


@pytest.mark.parametrize("version", [35, 38, 39])
def test_reads_every_literal_in_order_when_lengths_follow_from_the_next_entry(version):
    assert metadata_string_literals(metadata(SAMPLES, version)) == SAMPLES


def test_reads_an_empty_table():
    assert metadata_string_literals(metadata([])) == []


def test_refuses_a_file_without_the_metadata_signature():
    with pytest.raises(ValueError, match="encrypted"):
        metadata_string_literals(metadata(SAMPLES, sanity=0x12345678))


@pytest.mark.parametrize("version", [16, 23, 40])
def test_refuses_an_unsupported_version_by_number(version):
    with pytest.raises(ValueError, match=f"version {version}"):
        metadata_string_literals(metadata(SAMPLES, version))


def test_refuses_a_truncated_file():
    whole = metadata(SAMPLES)
    with pytest.raises(ValueError, match="past the end"):
        metadata_string_literals(whole[:-3])


def test_refuses_a_file_too_short_for_a_header():
    with pytest.raises(ValueError, match="IL2CPP metadata"):
        metadata_string_literals(b"\xaf\x1b")


def test_refuses_an_entry_outside_the_string_data():
    broken = bytearray(metadata(["はい"]))
    struct.pack_into("<I", broken, 256 + 4, 1000)  # the only entry's data index
    with pytest.raises(ValueError, match="outside"):
        metadata_string_literals(bytes(broken))
