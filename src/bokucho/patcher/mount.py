"""Install and restore native overlays through the shared patch executable."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from bokucho.patcher.core import (
    FolderState,
    PatchError,
    State,
    _refusal,
    _require_not_in_use,
    sha256_of,
)
from bokucho.patcher.payload import Patch, safe_parts

BACKUP = ".bokucho-backup"
REGISTRATION = b"""\r\n/* Bokucho translation: begin */
$plugins = $plugins.filter(function(plugin) { return plugin.name !== "Bokucho"; });
$plugins.push({name: "Bokucho", status: true, description: "Bokucho translation", parameters: {}});
/* Bokucho translation: end */\r\n"""

RPGMAKER = "rpgmaker"
RENPY = "renpy"


@dataclass(frozen=True)
class MountText:
    """What the player reads about a mounted patch: the game data, installing, removing."""

    names: str
    installing: str
    removing: str


_TEXT = {
    RPGMAKER: MountText(
        "RPG Maker 遊戲資料", "正在安裝翻譯 plugin。", "正在移除翻譯 plugin 並還原設定。"
    ),
    RENPY: MountText("Ren'Py 遊戲資料", "正在安裝 Ren'Py 翻譯檔。", "正在移除 Ren'Py 翻譯檔。"),
}


def mount_kind(patch: Patch) -> str:
    """Which overlay ``patch`` installs; one naming none predates Ren'Py's and is RPG Maker's."""
    assert patch.mount is not None
    try:
        with ZipFile(io.BytesIO(patch.mount)) as archive:
            kind = json.loads(archive.read("mount.json")).get("kind", RPGMAKER)
    except (OSError, ValueError, KeyError, AttributeError, BadZipFile):
        return RPGMAKER
    return RENPY if kind == RENPY else RPGMAKER


def mount_text(patch: Patch) -> MountText:
    return _TEXT[mount_kind(patch)]


def _target(folder: Path, name: str) -> Path:
    parts = safe_parts(name)
    if not parts:
        raise ValueError("Empty patch path")
    root = folder.resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Patch path leaves the game folder: {name}")
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink() or getattr(candidate, "is_junction", lambda: False)():
            raise ValueError(f"Patch path uses a link: {name}")
    return path


def _asset(name: str, base: str) -> bool:
    safe_parts(name)
    return (
        name == base + "js/plugins/Bokucho.js"
        or name.startswith(base + "bokucho/img/")
        and Path(name).suffix.lower() in {".png", ".rpgmvp", ".png_"}
    )


def _hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _metadata(data: bytes) -> dict:
    with ZipFile(io.BytesIO(data)) as archive:
        metadata = json.loads(archive.read("mount.json"))
        registry = metadata["registry"]
        if registry not in {"js/plugins.js", "www/js/plugins.js"}:
            raise ValueError("Invalid plugin registry")
        base = registry.removesuffix("js/plugins.js")
        names = archive.namelist()
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate overlay path")
        for name in names:
            if name != "mount.json" and not _asset(name, base):
                raise ValueError(f"Unexpected overlay file: {name}")
        if base + "js/plugins/Bokucho.js" not in names:
            raise ValueError("The translation plugin is missing")
        checks = metadata["checks"]
        if not isinstance(checks, dict) or base + "data/System.json" not in checks:
            raise ValueError("The game identity is missing")
        for name, digest in checks.items():
            if (
                len(safe_parts(name)) != len(safe_parts(base + "data/System.json"))
                or not name.startswith(base + "data/")
                or not name.endswith(".json")
                or not _hash(digest)
            ):
                raise ValueError("Invalid game identity")
        return metadata


def _record(folder: Path) -> dict | None:
    path = _target(folder, BACKUP + "/state.json")
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["registry"] not in {"js/plugins.js", "www/js/plugins.js"}:
        raise ValueError("Invalid restore registry")
    if (
        not _hash(record["payload"])
        or not _hash(record["original"])
        or not isinstance(record["created"], list)
        or not isinstance(record["directories"], list)
    ):
        raise ValueError("Invalid restore record")
    base = record["registry"].removesuffix("js/plugins.js")
    for entry in record["created"]:
        if not _asset(entry["name"], base) or not _hash(entry["sha256"]):
            raise ValueError("Invalid restore asset")
        _target(folder, entry["name"])
    for name in record["directories"]:
        if not (
            name == base + "js/plugins"
            or name == base + "bokucho"
            or name.startswith(base + "bokucho/")
        ):
            raise ValueError("Invalid restore directory")
        _target(folder, name)
    original = _target(folder, BACKUP + "/registry.bin")
    if not original.is_file() or sha256_of(original) != record["original"]:
        raise ValueError("The plugin registry backup is damaged")
    return record


def inspect_mount(folder: Path, patch: Patch) -> FolderState:
    assert patch.mount is not None
    if mount_kind(patch) == RENPY:
        from bokucho.patcher.renpy_mount import inspect_renpy

        return inspect_renpy(folder, patch)
    try:
        metadata = _metadata(patch.mount)
        record = _record(folder)
        if record is not None:
            registry = _target(folder, record["registry"])
            with ZipFile(io.BytesIO(patch.mount)) as archive:
                assets_match = all(
                    _target(folder, name).is_file()
                    and _target(folder, name).read_bytes() == archive.read(name)
                    for name in archive.namelist()
                    if name != "mount.json"
                )
            installed = (
                record["payload"] == hashlib.sha256(patch.mount).hexdigest()
                and registry.is_file()
                and REGISTRATION in registry.read_bytes()
                and assets_match
            )
            return FolderState(State.PATCHED if installed else State.UNKNOWN, True)
        if not _target(folder, metadata["registry"]).is_file():
            return FolderState(State.MISSING, False)
        for name, digest in metadata["checks"].items():
            path = _target(folder, name)
            if not path.is_file():
                return FolderState(State.MISSING, False)
            if sha256_of(path) != digest:
                return FolderState(State.UNKNOWN, False)
        return FolderState(State.ORIGINAL, False)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        return FolderState(State.UNKNOWN, False)


def _write_registry(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".bokucho-tmp")
    stream = temporary.open("xb")
    try:
        with stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_mount(folder: Path, patch: Patch) -> None:
    assert patch.mount is not None
    if mount_kind(patch) == RENPY:
        from bokucho.patcher.renpy_mount import install_renpy

        install_renpy(folder, patch)
        return
    created: list[Path] = []
    directories: list[Path] = []
    backup_files: list[Path] = []
    backup = None
    try:
        if inspect_mount(folder, patch).state is not State.ORIGINAL:
            raise PatchError("不是補丁對應的原版，或已套用補丁。請先還原，再重新套用。")
        metadata = _metadata(patch.mount)
        registry = _target(folder, metadata["registry"])
        _require_not_in_use(registry)
        original = registry.read_bytes()
        if REGISTRATION in original:
            raise PatchError("Bokucho 註冊已存在，請先還原。")
        backup = _target(folder, BACKUP)
        if backup.exists():
            raise PatchError(".bokucho-backup 已存在，請先還原或移到別處。")
        planned = _planned_assets(folder, patch.mount)
        needed = sum(len(data) for _, _, data in planned)
        if shutil.disk_usage(folder).free < needed + 2 * len(original) + (64 << 20):
            raise PatchError("磁碟空間不足，無法保留補丁與還原備份。")
        backup.mkdir()
        for name, data in (("registry.bin", original), ("state.json", b"")):
            path = backup / name
            backup_files.append(path)
            path.write_bytes(data)
        for _, path, data in planned:
            _create_with_parents(path, data, created, directories)
        record = {
            "payload": hashlib.sha256(patch.mount).hexdigest(),
            "registry": metadata["registry"],
            "original": hashlib.sha256(original).hexdigest(),
            "created": [
                {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
                for name, _, data in planned
            ],
            "directories": [path.relative_to(folder.resolve()).as_posix() for path in directories],
        }
        (backup / "state.json").write_text(json.dumps(record), encoding="utf-8")
        _write_registry(registry, original + REGISTRATION)
    except BaseException as error:
        try:
            _undo_install(created, directories, backup_files, backup)
        except OSError as cleanup:
            raise PatchError(
                f"安裝失敗且暫存檔無法清除，請保留 .bokucho-backup。（{cleanup}）"
            ) from error
        if isinstance(error, PatchError):
            raise
        if isinstance(error, PermissionError):
            raise _refusal(error, folder) from error
        raise PatchError(f"補丁安裝失敗，遊戲檔案沒有被改動。（{error}）") from error


def _planned_assets(folder: Path, mount: bytes) -> list[tuple[str, Path, bytes]]:
    """The mount's files the folder lacks; one it holds already must be the same."""
    with ZipFile(io.BytesIO(mount)) as archive:
        files = [(name, archive.read(name)) for name in archive.namelist() if name != "mount.json"]
    planned = []
    for name, data in files:
        path = _target(folder, name)
        if path.exists():
            if not path.is_file() or path.read_bytes() != data:
                raise PatchError(f"「{name}」已存在且內容不同，請先移到別處。")
        else:
            planned.append((name, path, data))
    return planned


def _create_with_parents(
    path: Path, data: bytes, created: list[Path], directories: list[Path]
) -> None:
    """Write a new file, noting it and each folder made for it so a failure can undo them."""
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir()
        directories.append(parent)
    with path.open("xb") as stream:
        created.append(path)
        stream.write(data)


def _undo_install(
    created: list[Path],
    directories: list[Path],
    backup_files: list[Path],
    backup: Path | None,
) -> None:
    for path in reversed(created):
        path.unlink(missing_ok=True)
    for path in reversed(directories):
        path.rmdir()
    for path in reversed(backup_files):
        path.unlink(missing_ok=True)
    if backup_files and backup is not None:
        backup.rmdir()


def restore_mount(folder: Path, patch: Patch) -> list[str]:
    """Restore through the same executable that applies the patch."""
    if mount_kind(patch) == RENPY:
        from bokucho.patcher.renpy_mount import restore_renpy

        return restore_renpy(folder)
    try:
        record = _record(folder)
        if record is None:
            raise PatchError("找不到有效的 .bokucho-backup 還原備份。")
        registry = _target(folder, record["registry"])
        original = _target(folder, BACKUP + "/registry.bin").read_bytes()
        if registry.exists():
            _require_not_in_use(registry)
            current = registry.read_bytes()
            if (
                REGISTRATION not in current
                and current != original
                and hashlib.sha256(current).hexdigest() != record.get("restored")
            ):
                raise PatchError("Bokucho 註冊區塊已被修改，請保留 .bokucho-backup，無法自動還原。")
            restored = current.replace(REGISTRATION, b"", 1)
        else:
            restored = original
        record["restored"] = hashlib.sha256(restored).hexdigest()
        _write_registry(_target(folder, BACKUP + "/state.json"), json.dumps(record).encode("utf-8"))
        _write_registry(registry, restored)
        left = []
        for entry in record["created"]:
            path = _target(folder, entry["name"])
            if path.exists():
                if path.is_file() and sha256_of(path) == entry["sha256"]:
                    path.unlink()
                else:
                    left.append(entry["name"])
        for name in reversed(record["directories"]):
            path = _target(folder, name)
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        backup = _target(folder, BACKUP)
        for name in ("state.json", "registry.bin"):
            (backup / name).unlink()
        if not any(backup.iterdir()):
            backup.rmdir()
        return left
    except PatchError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PatchError(f"還原失敗，請保留 .bokucho-backup 再試一次。（{error}）") from error
