"""Install and restore a Unity translation (BepInEx + XUnity.AutoTranslator) via the patcher.

The patch carries the translation; BepInEx and XUnity.AutoTranslator are downloaded
for the player's game when it is installed (see ``unity_loader``).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from bokucho.patcher.core import (
    SPARE_BYTES,
    FolderState,
    Log,
    PatchError,
    State,
    _refusal,
    sha256_of,
)
from bokucho.patcher.mount import BACKUP, UNITY, _hash, _target
from bokucho.patcher.payload import Patch, safe_parts
from bokucho.patcher.unity_loader import fetch_loader

#: The files BepInEx's doorstop loader keeps beside the game executable.
ROOT_FILES = frozenset({"winhttp.dll", "doorstop_config.ini", ".doorstop_version", "changelog.txt"})
LOADER = "BepInEx"
#: The .NET runtime BepInEx 6 brings for an IL2CPP game; nothing else writes in it.
RUNTIME = "dotnet"
#: The folders a patch creates beside the game executable.
_FOLDERS = (LOADER, RUNTIME)
BACKENDS = frozenset({"mono", "il2cpp"})
#: Where other mods live: never removed with the patch.
_MOD_FOLDERS = (("BepInEx", "plugins"), ("BepInEx", "patchers"))
#: Where every BepInEx mod keeps its settings; only the loader's own goes with the patch.
_CONFIG = ("BepInEx", "config")
#: What the patch's own plugins write in ``config`` the first time they run: BepInEx's
#: settings, and a file per XUnity plugin named for its GUID (``gravydevsupreme.xunity.*``).
_OWN_CONFIGS = ("BepInEx/config/bepinex.cfg", "BepInEx/config/gravydevsupreme.xunity.")


def _own_config(name: str) -> bool:
    lowered = name.casefold()
    return any(lowered.startswith(prefix.casefold()) for prefix in _OWN_CONFIGS)


def _overlay_name(name: str) -> bool:
    parts = safe_parts(name)
    return name in ROOT_FILES or (len(parts) >= 2 and parts[0] in _FOLDERS)


def _data_folder(name: object) -> bool:
    return isinstance(name, str) and len(safe_parts(name)) == 1 and name.endswith("_Data")


def _contents(data: bytes) -> tuple[str, str, list[tuple[str, bytes]]]:
    """The game data folder the patch was made for, its backend, and the translation files."""
    with ZipFile(io.BytesIO(data)) as archive:
        metadata = json.loads(archive.read("mount.json"))
        backend = metadata.get("backend", "mono")
        if (
            metadata.get("kind") != UNITY
            or not _data_folder(metadata.get("data"))
            or backend not in BACKENDS
        ):
            raise ValueError("Invalid Unity patch")
        names = [name for name in archive.namelist() if name != "mount.json"]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate overlay path")
        for name in names:
            if not _overlay_name(name):
                raise ValueError(f"Unexpected overlay file: {name}")
        if not any(name.endswith("/Text/Bokucho.txt") for name in names):
            raise ValueError("The translation is missing")
        return metadata["data"], backend, [(name, archive.read(name)) for name in names]


def _record(folder: Path) -> dict | None:
    path = _target(folder, BACKUP + "/state.json")
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if (
        record["kind"] != UNITY
        or not _hash(record["payload"])
        or not isinstance(record["created"], list)
    ):
        raise ValueError("Invalid restore record")
    for entry in record["created"]:
        if not _overlay_name(entry["name"]) or not _hash(entry["sha256"]):
            raise ValueError("Invalid restore file")
        _target(folder, entry["name"])
    return record


def inspect_unity(folder: Path, patch: Patch) -> FolderState:
    assert patch.mount is not None
    try:
        data, backend, files = _contents(patch.mount)
        record = _record(folder)
        if record is not None:
            installed = (
                record["payload"] == hashlib.sha256(patch.mount).hexdigest()
                and all(_target(folder, entry["name"]).is_file() for entry in record["created"])
                and all(
                    _target(folder, name).read_bytes() == content for name, content in files
                )
            )
            return FolderState(State.PATCHED if installed else State.UNKNOWN, True)
        game_data = _target(folder, data)
        if not _target(folder, "UnityPlayer.dll").is_file() or not (
            (game_data / "globalgamemanagers").is_file() or (game_data / "data.unity3d").is_file()
        ):
            return FolderState(State.MISSING, False)
        # A Mono patch's BepInEx cannot load an IL2CPP game, nor the other way round.
        if _target(folder, "GameAssembly.dll").is_file() != (backend == "il2cpp"):
            return FolderState(State.MISSING, False)
        if any(_target(folder, name).exists() for name in _FOLDERS) or any(
            _target(folder, name).exists() for name in ROOT_FILES
        ):
            return FolderState(State.UNKNOWN, False)
        return FolderState(State.ORIGINAL, False)
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        return FolderState(State.UNKNOWN, False)


def install_unity(folder: Path, patch: Patch, log: Log | None = None) -> None:
    assert patch.mount is not None
    created: list[Path] = []
    directories: list[Path] = []
    backup = None
    state = None
    try:
        if inspect_unity(folder, patch).state is not State.ORIGINAL:
            raise PatchError(
                "不是補丁對應的 Unity 遊戲，或遊戲已安裝 BepInEx 或其他模組。請先移除，再重新套用。"
            )
        data, backend, translation = _contents(patch.mount)
        backup = _target(folder, BACKUP)
        if backup.exists():
            raise PatchError(".bokucho-backup 已存在，請先還原或移到別處。")
        player = _target(folder, "UnityPlayer.dll")
        loader = fetch_loader(_target(folder, data), player, backend, log)
        unexpected = [name for name in loader if not _overlay_name(name)]
        if unexpected:
            raise PatchError(
                f"下載的 BepInEx 有預期之外的檔案：{unexpected[0]}，遊戲檔案沒有被改動。"
            )
        files = {**loader, **dict(translation)}
        planned = [(name, _target(folder, name), content) for name, content in files.items()]
        needed = sum(len(content) for *_, content in planned) + SPARE_BYTES
        if shutil.disk_usage(folder).free < needed:
            raise PatchError("磁碟空間不足，無法安裝補丁。")
        record = {
            "kind": UNITY,
            "payload": hashlib.sha256(patch.mount).hexdigest(),
            "created": [
                {"name": name, "sha256": hashlib.sha256(content).hexdigest()}
                for name, _, content in planned
            ],
        }
        backup.mkdir()
        state = backup / "state.json"
        state.write_text(json.dumps(record), encoding="utf-8")
        for _, path, content in planned:
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
                stream.write(content)
    except BaseException as error:
        try:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            for path in reversed(directories):
                path.rmdir()
            if state is not None:
                state.unlink(missing_ok=True)
                assert backup is not None
                backup.rmdir()
        except OSError as cleanup:
            raise PatchError(
                f"安裝失敗且暫存檔無法清除，請保留 .bokucho-backup。（{cleanup}）"
            ) from error
        if isinstance(error, PatchError):
            raise
        if isinstance(error, PermissionError):
            raise _refusal(error, folder) from error
        raise PatchError(f"補丁安裝失敗，遊戲檔案沒有被改動。（{error}）") from error


def restore_unity(folder: Path) -> list[str]:
    """Take away the loader and the translation; return the names kept: changed files and mods.

    BepInEx did not exist before the patch, so what it wrote since (logs, cache,
    its own settings, and an IL2CPP game's interop assemblies) goes with it;
    files under its plugins and patchers folders, and settings in its config
    folder, that the patch did not add are another mod's and stay. In
    ``dotnet`` only what the patch added goes.
    """
    try:
        record = _record(folder)
        if record is None:
            raise PatchError("找不到有效的 .bokucho-backup 還原備份。")
        added = {entry["name"]: entry["sha256"] for entry in record["created"]}
        left = []
        for name in sorted(ROOT_FILES & added.keys()):
            path = _target(folder, name)
            if path.is_file() and sha256_of(path) != added[name]:
                left.append(name)
            else:
                path.unlink(missing_ok=True)
        loader = _target(folder, LOADER)
        if loader.is_dir():
            root = folder.resolve()
            for path in sorted(loader.rglob("*"), reverse=True):
                name = path.relative_to(root).as_posix()
                if path.is_dir():
                    if not any(path.iterdir()):
                        path.rmdir()
                elif name in added or _own_config(name):
                    path.unlink()
                elif safe_parts(name)[:2] in (*_MOD_FOLDERS, _CONFIG):
                    left.append(name)
                else:
                    path.unlink()
            if not any(loader.iterdir()):
                loader.rmdir()
        runtime = _target(folder, RUNTIME)
        if runtime.is_dir():
            root = folder.resolve()
            for path in sorted(runtime.rglob("*"), reverse=True):
                name = path.relative_to(root).as_posix()
                if path.is_dir():
                    if not any(path.iterdir()):
                        path.rmdir()
                elif name in added and sha256_of(path) == added[name]:
                    path.unlink()
                else:
                    left.append(name)
            if not any(runtime.iterdir()):
                runtime.rmdir()
        backup = _target(folder, BACKUP)
        (backup / "state.json").unlink()
        if not any(backup.iterdir()):
            backup.rmdir()
        return left
    except PatchError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PatchError(f"還原失敗，請保留 .bokucho-backup 再試一次。（{error}）") from error
