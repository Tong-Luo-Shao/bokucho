"""Install and restore a Ren'Py translation through the shared patch executable."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from bokucho.patcher.core import SPARE_BYTES, FolderState, PatchError, State, _refusal, sha256_of
from bokucho.patcher.mount import BACKUP, RENPY, _hash, _target
from bokucho.patcher.payload import Patch, safe_parts

LANGUAGE_SCRIPT = "game/bokucho_language.rpy"
LANGUAGE = re.compile(r"[A-Za-z0-9_]+")


def language_script(language: str) -> str:
    return f"init 999 python:\n    config.language = {language!r}\n"


def _compiled(path: Path) -> Path:
    """Where Ren'Py writes the compiled form of a script it has loaded."""
    return path.with_name(path.name + "c")


def _overlay_name(name: str) -> bool:
    parts = safe_parts(name)
    return name == LANGUAGE_SCRIPT or (
        len(parts) >= 4 and parts[:2] == ("game", "tl") and name.endswith(".rpy")
    )


def _contents(data: bytes) -> tuple[str, list[tuple[str, bytes]]]:
    """The patch's language and the scripts it adds."""
    with ZipFile(io.BytesIO(data)) as archive:
        metadata = json.loads(archive.read("mount.json"))
        language = metadata["language"]
        if (
            metadata.get("kind") != RENPY
            or not isinstance(language, str)
            or not LANGUAGE.fullmatch(language)
        ):
            raise ValueError("Invalid Ren'Py language")
        names = [name for name in archive.namelist() if name != "mount.json"]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate overlay path")
        prefix = f"game/tl/{language}/"
        for name in names:
            if not _overlay_name(name) or (name != LANGUAGE_SCRIPT and not name.startswith(prefix)):
                raise ValueError(f"Unexpected overlay file: {name}")
        if LANGUAGE_SCRIPT not in names:
            raise ValueError("The language script is missing")
        return language, [(name, archive.read(name)) for name in names]


def _record(folder: Path) -> dict | None:
    path = _target(folder, BACKUP + "/state.json")
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if (
        record["kind"] != RENPY
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
        if safe_parts(name)[:2] != ("game", "tl"):
            raise ValueError("Invalid restore directory")
        _target(folder, name)
    return record


def inspect_renpy(folder: Path, patch: Patch) -> FolderState:
    assert patch.mount is not None
    try:
        language, files = _contents(patch.mount)
        record = _record(folder)
        if record is not None:
            installed = record["payload"] == hashlib.sha256(patch.mount).hexdigest() and all(
                _target(folder, name).is_file() and _target(folder, name).read_bytes() == data
                for name, data in files
            )
            return FolderState(State.PATCHED if installed else State.UNKNOWN, True)
        if not _target(folder, "game").is_dir() or not _target(folder, "renpy").is_dir():
            return FolderState(State.MISSING, False)
        script = _target(folder, LANGUAGE_SCRIPT)
        if (
            _target(folder, f"game/tl/{language}").exists()
            or script.exists()
            or _compiled(script).exists()
        ):
            return FolderState(State.UNKNOWN, False)
        return FolderState(State.ORIGINAL, False)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        return FolderState(State.UNKNOWN, False)


def install_renpy(folder: Path, patch: Patch) -> None:
    assert patch.mount is not None
    created: list[Path] = []
    directories: list[Path] = []
    state = None
    try:
        if inspect_renpy(folder, patch).state is not State.ORIGINAL:
            raise PatchError(
                "不是補丁對應的 Ren'Py 遊戲，或已有同語言的翻譯或已套用補丁。請先還原，再重新套用。"
            )
        _, files = _contents(patch.mount)
        backup = _target(folder, BACKUP)
        if backup.exists():
            raise PatchError(".bokucho-backup 已存在，請先還原或移到別處。")
        planned = [(name, _target(folder, name), data) for name, data in files]
        if shutil.disk_usage(folder).free < sum(len(data) for *_, data in planned) + SPARE_BYTES:
            raise PatchError("磁碟空間不足，無法安裝補丁。")
        new_directories = _new_directories(path for _, path, _ in planned)
        record = {
            "kind": RENPY,
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


def _new_directories(paths: Iterable[Path]) -> list[Path]:
    """The folders to make for ``paths``, each before the ones inside it."""
    new_directories: list[Path] = []
    for path in paths:
        chain = []
        parent = path.parent
        while not parent.exists() and parent not in new_directories and parent not in chain:
            chain.append(parent)
            parent = parent.parent
        new_directories.extend(reversed(chain))
    return new_directories


def _undo_install(created: list[Path], directories: list[Path], state: Path | None) -> None:
    for path in reversed(created):
        path.unlink(missing_ok=True)
    for path in reversed(directories):
        path.rmdir()
    if state is not None:
        state.unlink(missing_ok=True)
        state.parent.rmdir()


def restore_renpy(folder: Path) -> list[str]:
    """Take away what the patch added; return the names changed since, which stay."""
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
            _compiled(path).unlink(missing_ok=True)
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
