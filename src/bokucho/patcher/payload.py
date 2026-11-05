"""What a patch executable carries after the program: a manifest and the differences."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath

MAGIC = b"BOKUCHO1"
FORMAT = 1
CODEC = "zstd-prefix"
_LENGTH = struct.Struct("<Q")
_TRAILER = struct.Struct("<8sQ")


class NotAPatch(ValueError):
    """The file ends in no patch, or in one cut short."""


def safe_parts(name: str) -> tuple[str, ...]:
    """Only ordinary relative file names, on both POSIX and Windows."""
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or str(path) != name
        or any(part in {".", ".."} for part in path.parts)
        or any(
            ":" in part or part.endswith((".", " ")) or PureWindowsPath(part).is_reserved()
            for part in path.parts
        )
    ):
        raise ValueError(f"Unsafe recipe path: {name!r}")
    return path.parts


@dataclass(frozen=True)
class FileState:
    size: int
    sha256: str


@dataclass(frozen=True)
class PatchedFile:
    name: str
    original: FileState
    patched: FileState | None
    delta: bytes
    window_log: int


@dataclass(frozen=True)
class PatchAdded:
    """A file the patch puts beside the game: UIF, its configuration, a font."""

    name: str
    data: bytes


@dataclass(frozen=True)
class Patch:
    title: str
    files: tuple[PatchedFile, ...]
    added: tuple[PatchAdded, ...] = ()
    recipe: bytes | None = None
    mount: bytes | None = None


def patch_executable(program: bytes, patch: Patch) -> bytes:
    """``program`` with ``patch`` after it."""
    manifest = json.dumps(_manifest(patch), ensure_ascii=False).encode("utf-8")
    payload = b"".join(
        [
            _LENGTH.pack(len(manifest)),
            manifest,
            *(item.delta for item in patch.files),
            *(item.data for item in patch.added),
            *([patch.recipe] if patch.recipe is not None else []),
            *([patch.mount] if patch.mount is not None else []),
        ]
    )
    return program + payload + _TRAILER.pack(MAGIC, len(payload))


def _manifest(patch: Patch) -> dict:
    """Where each part of the payload sits after the manifest, and what it must hash to."""
    if patch.mount is not None and (patch.files or patch.added or patch.recipe is not None):
        raise ValueError("A mounted patch cannot also contain a binary patch or recipe.")
    entries = []
    offset = 0
    for item in patch.files:
        entries.append(_file_entry(item, offset))
        offset += len(item.delta)
    document: dict = {"format": FORMAT, "title": patch.title, "files": entries}
    added = []
    for item in patch.added:
        added.append({"name": item.name, **_blob(item.data, offset)})
        offset += len(item.data)
    if added:
        document["added"] = added
    if patch.recipe is not None:
        document["format"] = 2
        document["recipe"] = _blob(patch.recipe, offset)
    if patch.mount is not None:
        document["format"] = 3
        document["mount"] = _blob(patch.mount, offset)
    return document


def _file_entry(item: PatchedFile, offset: int) -> dict:
    return {
        "name": item.name,
        "original": {"size": item.original.size, "sha256": item.original.sha256},
        "patched": (
            {"size": item.patched.size, "sha256": item.patched.sha256}
            if item.patched is not None
            else None
        ),
        "delta": {
            "offset": offset,
            "size": len(item.delta),
            "codec": CODEC,
            "window_log": item.window_log,
        },
    }


def _blob(data: bytes, offset: int) -> dict:
    return {"offset": offset, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def read_patch(data: bytes) -> Patch:
    """The patch at the end of ``data``."""
    if len(data) < _TRAILER.size:
        raise NotAPatch("The file is too short to hold a patch.")
    end = len(data) - _TRAILER.size
    magic, length = _TRAILER.unpack_from(data, end)
    if magic != MAGIC or length > end:
        raise NotAPatch("The file ends in no patch.")
    payload = memoryview(data)[end - length : end]
    try:
        (manifest_length,) = _LENGTH.unpack_from(payload, 0)
        manifest = json.loads(bytes(payload[_LENGTH.size : _LENGTH.size + manifest_length]))
        blobs = payload[_LENGTH.size + manifest_length :]
        if manifest["format"] not in {FORMAT, 2, 3}:
            raise NotAPatch(f"Patch format {manifest['format']} is not one this program reads.")
        files = tuple(
            _patched_file(entry, blobs, manifest["format"] == 2) for entry in manifest["files"]
        )
        added = tuple(_added_file(entry, blobs) for entry in manifest.get("added", []))
        recipe = None
        mount = None
        if manifest["format"] == 2:
            recipe = _added_file({**manifest["recipe"], "name": "recipe.zip"}, blobs).data
            if not files or added or any(item.patched is not None or item.delta for item in files):
                raise NotAPatch("A compilation recipe must name only the original game files.")
        elif manifest["format"] == 3:
            mount = _added_file({**manifest["mount"], "name": "mount.zip"}, blobs).data
            if files or added or "recipe" in manifest:
                raise NotAPatch("A mounted patch must contain only its overlay.")
        elif any(item.patched is None for item in files):
            raise NotAPatch("A binary patch must name its result.")
        if mount is None and (
            not files or len({item.name.lower() for item in files}) != len(files)
        ):
            raise NotAPatch("The patch must name distinct game files.")
        return Patch(str(manifest["title"]), files, added, recipe, mount)
    except NotAPatch:
        raise
    except (KeyError, TypeError, ValueError, struct.error) as error:
        raise NotAPatch(f"The patch cannot be read: {error}") from error


def _bare_name(name: str) -> bool:
    """Written as folder / name: anything but a bare file name is a path elsewhere."""
    return (
        name not in {"", ".", ".."}
        and PurePath(name).name == name
        and "/" not in name
        and "\\" not in name
    )


def _nested_name(name: str) -> bool:
    try:
        safe_parts(name)
    except ValueError:
        return False
    return True


def _patched_file(entry: dict, blobs: memoryview, nested: bool = False) -> PatchedFile:
    name = str(entry["name"])
    if not (_nested_name(name) if nested else _bare_name(name)):
        raise NotAPatch(f"The patch names a file outside the game folder: {name!r}")
    delta = entry["delta"]
    start, size = int(delta["offset"]), int(delta["size"])
    if delta["codec"] != CODEC or start < 0 or start + size > len(blobs):
        raise NotAPatch(f"The difference for {name} is not in the file.")
    return PatchedFile(
        name,
        FileState(int(entry["original"]["size"]), str(entry["original"]["sha256"])),
        (
            FileState(int(entry["patched"]["size"]), str(entry["patched"]["sha256"]))
            if entry.get("patched") is not None
            else None
        ),
        bytes(blobs[start : start + size]),
        int(delta["window_log"]),
    )


def _added_file(entry: dict, blobs: memoryview) -> PatchAdded:
    name = str(entry["name"])
    if not _bare_name(name):
        raise NotAPatch(f"The patch adds a file outside the game folder: {name!r}")
    start, size = int(entry["offset"]), int(entry["size"])
    if start < 0 or start + size > len(blobs):
        raise NotAPatch(f"{name} is not in the file.")
    data = bytes(blobs[start : start + size])
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise NotAPatch(f"{name} is damaged.")
    return PatchAdded(name, data)
