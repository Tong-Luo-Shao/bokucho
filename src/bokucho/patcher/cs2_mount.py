"""Install and restore a CatSystem2 localization overlay through the shared patch executable.

CatSystem2-on-Ren'Py games read their on-screen text at runtime from
``game/data/localization/<lang>.json``, which usually ships inside a ``.rpa``
archive. A loose file of the same path overrides the archived one, so the patch
is a handful of loose JSON overlays, installed, inspected and removed the same
way the Ren'Py translation overlay is, only without the language script and the
compiled ``.rpyc`` siblings.

ponytail: install_cs2/restore_cs2 deliberately mirror renpy_mount's loose-file
flow (the repo keeps one such module per engine kind). Only the overlay
allowlist, the mount metadata and the restore record differ; the generic
directory helpers are reused from renpy_mount rather than copied.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from bokucho.patcher.core import SPARE_BYTES, FolderState, PatchError, State, _refusal, sha256_of
from bokucho.patcher.mount import BACKUP, CS2, _hash, _target
from bokucho.patcher.payload import Patch, safe_parts
from bokucho.patcher.renpy_mount import _new_directories, _undo_install

_LOC_DIR = "game/data/localization"


def _overlay_name(name: str) -> bool:
    parts = safe_parts(name)
    return (
        len(parts) == 4 and parts[:3] == ("game", "data", "localization") and name.endswith(".json")
    )


def _contents(data: bytes) -> list[tuple[str, bytes]]:
    """The localization overlays the patch adds."""
    with ZipFile(io.BytesIO(data)) as archive:
        metadata = json.loads(archive.read("mount.json"))
        if metadata.get("kind") != CS2:
            raise ValueError("Invalid CS2 mount")
        names = [name for name in archive.namelist() if name != "mount.json"]
        if not names:
            raise ValueError("The localization overlay is missing")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate overlay path")
        for name in names:
            if not _overlay_name(name):
                raise ValueError(f"Unexpected overlay file: {name}")
        return [(name, archive.read(name)) for name in names]


def _record(folder: Path) -> dict | None:
    path = _target(folder, BACKUP + "/state.json")
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if (
        record["kind"] != CS2
        or not _hash(record["payload"])
        or not isinstance(record["created"], list)
        or not isinstance(record["directories"], list)
    ):
        raise ValueError("Invalid restore record")
    for entry in record["created"]:
        if not _overlay_name(entry["name"]) or not _hash(entry["sha256"]):
            raise ValueError("Invalid restore file")
        _target(folder, entry["name"])
    for name in record["directories"]:
        if name not in ("game/data", _LOC_DIR):
            raise ValueError("Invalid restore directory")
        _target(folder, name)
    return record


def inspect_cs2(folder: Path, patch: Patch) -> FolderState:
    assert patch.mount is not None
    try:
        files = _contents(patch.mount)
        record = _record(folder)
        if record is not None:
            installed = record["payload"] == hashlib.sha256(patch.mount).hexdigest() and all(
                _target(folder, name).is_file() and _target(folder, name).read_bytes() == data
                for name, data in files
            )
            return FolderState(State.PATCHED if installed else State.UNKNOWN, True)
        if not _target(folder, "game").is_dir() or not _target(folder, "renpy").is_dir():
            return FolderState(State.MISSING, False)
        # The overlays ship inside a .rpa; a loose copy already in place is not ours to clobber.
        if any(_target(folder, name).exists() for name, _ in files):
            return FolderState(State.UNKNOWN, False)
        return FolderState(State.ORIGINAL, False)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        return FolderState(State.UNKNOWN, False)


def install_cs2(folder: Path, patch: Patch) -> None:
    assert patch.mount is not None
    created: list[Path] = []
    directories: list[Path] = []
    state = None
    try:
        if inspect_cs2(folder, patch).state is not State.ORIGINAL:
            raise PatchError(
                "不是補丁對應的 CatSystem2 遊戲，或已有同名翻譯檔或已套用補丁。"
                "請先還原，再重新套用。"
            )
        files = _contents(patch.mount)
        backup = _target(folder, BACKUP)
        if backup.exists():
            raise PatchError(".bokucho-backup 已存在，請先還原或移到別處。")
        planned = [(name, _target(folder, name), data) for name, data in files]
        if shutil.disk_usage(folder).free < sum(len(data) for *_, data in planned) + SPARE_BYTES:
            raise PatchError("磁碟空間不足，無法安裝補丁。")
        new_directories = _new_directories(path for _, path, _ in planned)
        record = {
            "kind": CS2,
            "payload": hashlib.sha256(patch.mount).hexdigest(),
            "created": [
                {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
                for name, _, data in planned
            ],
            "directories": [
                path.relative_to(folder.resolve()).as_posix() for path in new_directories
            ],
        }
        backup.mkdir()
        state = backup / "state.json"
        state.write_text(json.dumps(record), encoding="utf-8")
        for path in new_directories:
            path.mkdir()
            directories.append(path)
        for _, path, data in planned:
            with path.open("xb") as stream:
                created.append(path)
                stream.write(data)
    except BaseException as error:
        try:
            _undo_install(created, directories, state)
        except OSError as cleanup:
            raise PatchError(
                f"安裝失敗且暫存檔無法清除，請保留 .bokucho-backup。（{cleanup}）"
            ) from error
        if isinstance(error, PatchError):
            raise
        if isinstance(error, PermissionError):
            raise _refusal(error, folder) from error
        raise PatchError(f"補丁安裝失敗，遊戲檔案沒有被改動。（{error}）") from error


def restore_cs2(folder: Path) -> list[str]:
    """Take away the overlays the patch added; return the names changed since, which stay."""
    try:
        record = _record(folder)
        if record is None:
            raise PatchError("找不到有效的 .bokucho-backup 還原備份。")
        left = []
        for entry in record["created"]:
            path = _target(folder, entry["name"])
            if path.exists():
                if not path.is_file() or sha256_of(path) != entry["sha256"]:
                    left.append(entry["name"])
                    continue
                path.unlink()
        for name in reversed(record["directories"]):
            path = _target(folder, name)
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        backup = _target(folder, BACKUP)
        (backup / "state.json").unlink()
        if not any(backup.iterdir()):
            backup.rmdir()
        return left
    except PatchError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PatchError(f"還原失敗，請保留 .bokucho-backup 再試一次。（{error}）") from error
