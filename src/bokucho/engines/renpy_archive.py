"""Read members out of a Ren'Py ``.rpa`` archive (RPA-2.0 / RPA-3.0).

The archive index is a pickle, and a game is untrusted input, so it is loaded
with an unpickler that refuses every global -- a benign index is only dicts,
lists, tuples, ints, strings and bytes, which need no global, while a malicious
``__reduce__`` payload does and is turned away.
"""

from __future__ import annotations

import io
import pickle
import zlib
from pathlib import Path


class _SafeUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):  # noqa: D401 - pickle hook
        raise pickle.UnpicklingError(f"refused to load global {module}.{name}")


def _load_index(blob: bytes) -> dict:
    index = _SafeUnpickler(io.BytesIO(zlib.decompress(blob))).load()
    if not isinstance(index, dict):
        raise ValueError("archive index is not a mapping")
    return index


def rpa_index(path: str | Path) -> dict[str, list[tuple[int, int, bytes]]]:
    """Member name -> its segments as ``(offset, length, prefix)``, keys applied."""
    with open(path, "rb") as stream:
        parts = stream.readline().split()
        if not parts or parts[0] not in (b"RPA-3.0", b"RPA-2.0"):
            raise ValueError(
                f"{Path(path).name} is not an RPA-2.0/3.0 archive "
                f"(header {parts[0] if parts else b''!r})"
            )
        offset = int(parts[1], 16)
        key = int(parts[2], 16) if parts[0] == b"RPA-3.0" and len(parts) > 2 else 0
        stream.seek(offset)
        raw = _load_index(stream.read())
    index: dict[str, list[tuple[int, int, bytes]]] = {}
    for name, segments in raw.items():
        index[name] = [
            (seg[0] ^ key, seg[1] ^ key, _as_bytes(seg[2]) if len(seg) > 2 else b"")
            for seg in segments
        ]
    return index


def _as_bytes(prefix) -> bytes:
    return prefix.encode("latin1") if isinstance(prefix, str) else bytes(prefix)


def read_member(
    path: str | Path,
    name: str,
    index: dict[str, list[tuple[int, int, bytes]]] | None = None,
) -> bytes:
    """The bytes of one archive member (all its segments concatenated)."""
    index = index if index is not None else rpa_index(path)
    with open(path, "rb") as stream:
        out = bytearray()
        for offset, length, prefix in index[name]:
            out += prefix
            stream.seek(offset)
            out += stream.read(length - len(prefix))
    return bytes(out)
