"""RPG Maker MV/MZ asset encryption."""

from __future__ import annotations

import json
from pathlib import Path

HEADER = b"RPGMV\x00\x00\x00\x00\x03\x01\x00\x00\x00\x00\x00"

ENCRYPTED_SUFFIXES = frozenset({".rpgmvp", ".rpgmvo", ".rpgmvm", ".png_", ".ogg_", ".m4a_"})

_SCRAMBLED = 16


def encryption_key(data_dir: Path) -> str | None:
    """The game's asset key, or None when it ships its pictures in the clear."""
    system = json.loads((Path(data_dir) / "System.json").read_text(encoding="utf-8"))
    if not system.get("hasEncryptedImages"):
        return None
    key = system.get("encryptionKey")
    return str(key) if key else None


def _xor(payload: bytes, key: str) -> bytes:
    key_bytes = bytes.fromhex(key)
    body = bytearray(payload)
    for index in range(min(_SCRAMBLED, len(body))):
        body[index] ^= key_bytes[index % len(key_bytes)]
    return bytes(body)


def decrypt(blob: bytes, key: str) -> bytes:
    """The real file inside an encrypted asset."""
    return _xor(blob[len(HEADER) :], key)


def encrypt(blob: bytes, key: str) -> bytes:
    """``blob`` as the engine expects to find it on disk."""
    return HEADER + _xor(blob, key)


_SEARCH_DEPTH = 6


def key_for(path: Path) -> str | None:
    """The asset key of the game owning ``path``, or None if there is not one."""
    for parent in list(Path(path).parents)[:_SEARCH_DEPTH]:
        system = parent / "data" / "System.json"
        if system.is_file():
            try:
                return encryption_key(parent / "data")
            except (OSError, ValueError, AttributeError):
                return None
    return None


def open_bytes(path: Path, *, source: Path | None = None) -> bytes:
    """``path``'s real content, decrypted if the engine encrypted it."""
    blob = Path(path).read_bytes()
    if Path(path).suffix.lower() not in ENCRYPTED_SUFFIXES:
        return blob
    key = key_for(source if source is not None else path)
    return decrypt(blob, key) if key else blob
