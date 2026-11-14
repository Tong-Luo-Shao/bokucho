"""IL2CPP Unity builds: the C# string literals kept in ``global-metadata.dat``.

An IL2CPP game has no managed assemblies to read a ``#US`` heap from; the
compiler moves every string literal into one table of the metadata file.
"""

from __future__ import annotations

import struct

#: Where the metadata sits inside the game's ``*_Data`` folder.
METADATA = "il2cpp_data/Metadata/global-metadata.dat"
_SANITY = 0xFAB11BAF
#: Unity 2018.3 (24) through Unity 6 (39); 35 dropped the stored lengths, 38 added section counts.
_VERSIONS = range(24, 40)


def metadata_string_literals(metadata: bytes) -> list[str]:
    """The C# string literals of an IL2CPP game, in table order."""
    try:
        sanity, version = struct.unpack_from("<Ii", metadata)
        if sanity != _SANITY:
            raise ValueError(
                "no IL2CPP metadata signature; the file may be encrypted or obfuscated"
            )
        if version not in _VERSIONS:
            raise ValueError(
                f"IL2CPP metadata version {version} is not supported "
                f"({_VERSIONS.start} to {_VERSIONS.stop - 1} are)"
            )
        section = 12 if version >= 38 else 8
        table, table_size = struct.unpack_from("<ii", metadata, 8)
        data, data_size = struct.unpack_from("<ii", metadata, 8 + section)
        if min(table, table_size, data, data_size) < 0 or max(
            table + table_size, data + data_size
        ) > len(metadata):
            raise ValueError("the string literal table runs past the end of the file")
        entries = metadata[table : table + table_size]
        if version < 35:
            pairs = struct.iter_unpack("<II", entries)
            spans = [(start, start + length) for length, start in pairs]
        else:
            starts = [start for (start,) in struct.iter_unpack("<I", entries)]
            spans = list(zip(starts, [*starts[1:], data_size], strict=True))
    except struct.error as exc:
        raise ValueError(f"not readable IL2CPP metadata: {exc}") from exc
    blob = metadata[data : data + data_size]
    found: list[str] = []
    for start, end in spans:
        if not 0 <= start <= end <= data_size:
            raise ValueError(f"a string literal lies outside the string data ({start}..{end})")
        found.append(blob[start:end].decode("utf-8", "replace"))
    return found
