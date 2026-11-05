"""Applying a patch to a game folder, and undoing it: everything the window does, without it."""

from __future__ import annotations

import hashlib
import io
import json
import locale
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePath
from zipfile import ZipFile

from bokucho.patcher.delta import apply_delta
from bokucho.patcher.payload import FileState, Patch, PatchedFile, safe_parts

ERROR_SHARING_VIOLATION = 32
SPARE_BYTES = 64 << 20
_DAMAGED = "補丁檔已損壞，請重新下載。遊戲檔案沒有被改動。"
_NO_PERMISSION = (
    "沒有權限寫入「{folder}」。遊戲如果裝在 Program Files 這類系統資料夾，"
    "請把整個遊戲資料夾搬到其他地方（例如「文件」）再試一次。"
)
RECORD = "bokucho-added.json"
GAME_INI = "Game.ini"
_IN_USE = "有檔案正在使用中。請關閉遊戲和其他程式，再試一次。"
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
Log = Callable[[str], None]


def _report(log: Log | None, message: str) -> None:
    if log is not None:
        log(message)


class PatchError(Exception):
    """What the player needs to know; the game folder is as it was."""


class State(Enum):
    MISSING = "missing"
    ORIGINAL = "original"
    PATCHED = "patched"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FolderState:
    state: State
    restorable: bool


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def backup_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.bak")


def _temporary_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.bokucho-tmp")


def _names(patch: Patch) -> str:
    if patch.mount is not None:
        from bokucho.patcher.mount import mount_text

        return mount_text(patch).names
    names = [item.name for item in patch.files]
    if len(names) > 3:
        return f"{'、'.join(names[:3])} 等 {len(names)} 個檔案"
    return "、".join(names)


def _file_state(folder: Path, item: PatchedFile, patch: Patch) -> State:
    target = folder / item.name
    if not target.is_file():
        return State.MISSING
    digest = sha256_of(target)
    if digest == item.original.sha256:
        return State.ORIGINAL
    if item.patched is not None and digest == item.patched.sha256:
        return State.PATCHED
    if patch.recipe is not None:
        try:
            record = json.loads((folder / RECORD).read_text(encoding="utf-8"))
            if (
                record.get("recipe") == _digest(patch.recipe)
                and record.get("compiled", {}).get(item.name) == digest
            ):
                return State.PATCHED
        except (OSError, ValueError, AttributeError):
            pass
    return State.UNKNOWN


def _backup_is_original(folder: Path, item: PatchedFile) -> bool:
    backup = backup_path(folder / item.name)
    return backup.is_file() and sha256_of(backup) == item.original.sha256


def inspect(folder: Path, patch: Patch) -> FolderState:
    """Whether ``folder`` holds the originals, the translation, or neither."""
    if patch.mount is not None:
        from bokucho.patcher.mount import inspect_mount

        return inspect_mount(folder, patch)
    each = {item.name: _file_state(folder, item, patch) for item in patch.files}
    states = set(each.values())
    if State.MISSING in states:
        state = State.MISSING
    elif states == {State.ORIGINAL}:
        state = State.ORIGINAL
    elif states == {State.PATCHED}:
        state = State.PATCHED
    else:
        state = State.UNKNOWN
    restorable = state is not State.ORIGINAL and all(
        each[item.name] is State.ORIGINAL or _backup_is_original(folder, item)
        for item in patch.files
    )
    return FolderState(state, restorable)


def describe(found: FolderState, patch: Patch) -> str:
    """The status line for ``found``."""
    names = _names(patch)
    if found.state is State.ORIGINAL:
        return f"找到原版 {names}，可以套用。"
    if found.state is State.PATCHED:
        if found.restorable:
            return "已經套用過補丁。要換回原版請按「還原」。"
        return "已經套用過補丁，但找不到原版備份，無法還原。"
    if found.state is State.MISSING:
        if found.restorable:
            return f"找不到 {names}，但有原版備份。要換回原版請按「還原」。"
        return f"這個資料夾裡找不到 {names}。請選擇遊戲所在的資料夾。"
    if found.restorable:
        return f"{names} 不是這個補丁的版本，但有原版備份：請先按「還原」，再按「套用」。"
    return f"{names} 不是這個補丁對應的原版（版本不同，或已被其他補丁修改），所以不能套用。"


def default_folder(start: Path, patch: Patch) -> Path | None:
    """The folder the patch sits in, when the game is there too."""
    if patch.mount is not None:
        found = inspect(start, patch)
        return start if found.state in {State.ORIGINAL, State.PATCHED} or found.restorable else None
    return start if all((start / item.name).is_file() for item in patch.files) else None


def _open_for_update(path: Path) -> None:
    """Ask for write access the way Windows says who holds the file."""
    if sys.platform != "win32":
        with path.open("r+b"):
            return
    import _winapi

    handle = _winapi.CreateFile(str(path), _winapi.GENERIC_WRITE, 7, 0, _winapi.OPEN_EXISTING, 0, 0)
    _winapi.CloseHandle(handle)


def _require_not_in_use(target: Path) -> None:
    try:
        _open_for_update(target)
    except PermissionError as error:
        if getattr(error, "winerror", None) == ERROR_SHARING_VIOLATION:
            raise PatchError(f"{target.name} 正在使用中。請先關閉遊戲，再試一次。") from error


def _refusal(error: PermissionError, folder: Path) -> PatchError:
    if getattr(error, "winerror", None) == ERROR_SHARING_VIOLATION:
        return PatchError(_IN_USE)
    return PatchError(_NO_PERMISSION.format(folder=folder))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recorded(folder: Path) -> list[dict[str, str]]:
    """The files a patch added to this game; nothing for a record gone or damaged."""
    try:
        data = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("added", []) if isinstance(data, dict) else []
    return [
        {"name": entry["name"], "sha256": entry["sha256"]}
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("sha256"), str)
        and _inside(entry["name"])
    ]


def _inside(name: str) -> bool:
    """A relative path in the game folder: an RGSS fill font goes into its Fonts folder."""
    try:
        safe_parts(name)
    except ValueError:
        return False
    return True


def _record(folder: Path, patch: Patch) -> None:
    listed = _recorded(folder)
    for item in patch.added:
        entry = {"name": item.name, "sha256": _digest(item.data)}
        if entry not in listed:
            listed.append(entry)
    document = {"added": listed}
    if patch.recipe is not None:
        document["recipe"] = _digest(patch.recipe)
        document["compiled"] = {
            item.name: item.patched.sha256 for item in patch.files if item.patched is not None
        }
    (folder / RECORD).write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def apply(folder: Path, patch: Patch, log: Log | None = None) -> None:
    """Put the translation in place of each original, keeping the original as ``<name>.bak``."""
    _report(log, "正在檢查遊戲檔案。")
    if patch.mount is not None:
        from bokucho.patcher.mount import install_mount, mount_text

        _report(log, mount_text(patch).installing)
        install_mount(folder, patch)
        return
    if patch.recipe is None:
        _report(log, "正在套用翻譯檔案。")
        _install(folder, patch)
        return
    found = inspect(folder, patch)
    if found.state is not State.ORIGINAL:
        raise PatchError(describe(found, patch))
    for item in patch.files:
        target = folder / item.name
        _require_not_in_use(target)
        if backup_path(target).exists() and not _backup_is_original(folder, item):
            raise PatchError(f"「{backup_path(target).name}」不是原版備份，請先移到別處。")
    try:
        from bokucho.patcher.recipe import compile_recipe, recipe_size

        needed = sum(item.original.size for item in patch.files) * 4
        needed += recipe_size(patch.recipe) * 2 + SPARE_BYTES
        if shutil.disk_usage(folder).free < needed:
            raise PatchError("磁碟空間不足：編譯補丁需要解包與重建遊戲的暫存空間。")
        with tempfile.TemporaryDirectory(prefix=".bokucho-compile-", dir=folder) as scratch:
            rebuilt, added = compile_recipe(folder, patch, Path(scratch), log)
            files = tuple(
                replace(
                    item,
                    patched=FileState(
                        rebuilt[item.name].stat().st_size, sha256_of(rebuilt[item.name])
                    ),
                )
                for item in patch.files
            )
            compiled = replace(patch, files=files, added=added)
            _report(log, "正在安裝編譯結果。")
            _install(folder, compiled, rebuilt)
    except PatchError:
        raise
    except PermissionError as error:
        raise _refusal(error, folder) from error
    except Exception as error:
        raise PatchError(f"補丁編譯失敗，遊戲檔案沒有被改動。（{error}）") from error
    _retitle(folder, patch.recipe, log)


def _title_line(lines: list[bytes]) -> int | None:
    """Where ``[Game]``'s ``Title=`` is.

    A line's first byte, and every byte before its first ``=``, is ASCII in the code pages
    RGSS games are written in (CP932, Big5, GBK), so the lines are read without decoding.
    """
    section = b""
    for index, line in enumerate(lines):
        text = line.strip()
        key, equals, _ = text.partition(b"=")
        if text.startswith(b"["):
            section = text.strip(b"[] \t").lower()
        elif section == b"game" and equals and key.strip().lower() == b"title":
            return index
    return None


def _with_title(line: bytes, value: bytes) -> bytes:
    body = line.rstrip(b"\r\n")
    return body.partition(b"=")[0] + b"=" + value + line[len(body) :]


def _rewrite(path: Path, data: bytes) -> None:
    temporary = _temporary_path(path)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _retitle(folder: Path, recipe: bytes, log: Log | None) -> None:
    """Put the translated title in the window, which RPG Maker reads in the player's ANSI code page.

    Only the ``Title=`` line changes: the game writes its own settings into Game.ini as it runs.
    """
    with ZipFile(io.BytesIO(recipe)) as archive:
        title = json.loads(archive.read("recipe.json")).get("title")
    ini = folder / GAME_INI
    if not isinstance(title, str) or not title:
        return
    try:
        lines = ini.read_bytes().splitlines(keepends=True)
    except OSError:
        return
    index = _title_line(lines)
    if index is None:
        return
    encoding = locale.getencoding()
    try:
        value = title.encode(encoding)
    except UnicodeEncodeError:
        _report(log, f"這台電腦的系統編碼（{encoding}）顯示不了翻譯後的標題，視窗標題保留原文。")
        return
    original = lines[index].rstrip(b"\r\n").partition(b"=")[2]
    if value == original:
        return
    lines[index] = _with_title(lines[index], value)
    try:
        record = folder / RECORD
        document = json.loads(record.read_text(encoding="utf-8"))
        document["title"] = original.hex()
        record.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        _rewrite(ini, b"".join(lines))
    except (OSError, ValueError):
        _report(log, f"無法改寫 {GAME_INI} 的視窗標題，標題保留原文；翻譯已經套用。")


def _restore_title(folder: Path) -> None:
    """Put back the title :func:`_retitle` replaced, keeping the settings the game wrote since."""
    try:
        original = bytes.fromhex(
            json.loads((folder / RECORD).read_text(encoding="utf-8")).get("title")
        )
        lines = (folder / GAME_INI).read_bytes().splitlines(keepends=True)
    except (OSError, ValueError, AttributeError, TypeError):
        return
    index = _title_line(lines)
    if index is not None:
        lines[index] = _with_title(lines[index], original)
        _rewrite(folder / GAME_INI, b"".join(lines))


def _install(folder: Path, patch: Patch, prepared: dict[str, Path] | None = None) -> None:
    _refuse_install(folder, patch)
    written: list[Path] = []
    try:
        for item in patch.files:
            _stage(folder, item, prepared, written)
        _commit_install(folder, patch)
    except ValueError as error:
        raise PatchError(_DAMAGED) from error
    except PermissionError as error:
        raise _refusal(error, folder) from error
    finally:
        for temporary in written:
            temporary.unlink(missing_ok=True)


def _refuse_install(folder: Path, patch: Patch) -> None:
    """Stop before anything is written unless the folder holds the original, with room."""
    found = inspect(folder, patch)
    if found.state is not State.ORIGINAL:
        raise PatchError(describe(found, patch))
    for item in patch.files:
        target = folder / item.name
        _require_not_in_use(target)
        backup = backup_path(target)
        if backup.exists() and not _backup_is_original(folder, item):
            raise PatchError(
                f"「{backup.name}」已經存在，而且不是原版。請先把它移到別處，再試一次。"
            )
    for item in patch.added:
        existing = folder / item.name
        if existing.exists() and sha256_of(existing) != _digest(item.data):
            raise PatchError(
                f"遊戲資料夾裡已經有「{item.name}」，而且內容不同（可能是其他補丁的檔案）。"
                "請先把它移到別處，再試一次。"
            )
    needed = (
        sum(item.patched.size for item in patch.files if item.patched is not None)
        + sum(len(item.data) for item in patch.added)
        + SPARE_BYTES
    )
    if shutil.disk_usage(folder).free < needed:
        raise PatchError("磁碟空間不足：套用時需要再多一份遊戲檔案的空間。")


def _stage(
    folder: Path, item: PatchedFile, prepared: dict[str, Path] | None, written: list[Path]
) -> None:
    """Write the translated file beside the original, and check it came out as built."""
    target = folder / item.name
    temporary = _temporary_path(target)
    try:
        out = temporary.open("xb")
    except FileExistsError as error:
        raise PatchError(f"「{temporary.name}」已經存在，請先移到別處。") from error
    written.append(temporary)
    with out:
        if prepared is not None:
            with prepared[item.name].open("rb") as source:
                shutil.copyfileobj(source, out)
        else:
            digest = apply_delta(target.read_bytes(), item.delta, item.window_log, out)
    if prepared is not None:
        digest = sha256_of(temporary)
    assert item.patched is not None
    if digest != item.patched.sha256:
        raise PatchError(_DAMAGED)


def _commit_install(folder: Path, patch: Patch) -> None:
    """Add the new files and swap the staged ones in, or leave the folder as it was."""
    record = folder / RECORD
    before = record.read_bytes() if record.is_file() else None
    wrote: list[Path] = []
    try:
        for item in patch.added:
            path = folder / item.name
            if not path.exists():
                wrote.append(path)
                path.write_bytes(item.data)
        if patch.added or patch.recipe is not None:
            _record(folder, patch)
        _swap(folder, patch)
    except BaseException:
        for path in wrote:
            path.unlink(missing_ok=True)
        if patch.added or patch.recipe is not None:
            if before is None:
                record.unlink(missing_ok=True)
            else:
                record.write_bytes(before)
        raise


def _swap(folder: Path, patch: Patch) -> None:
    """Move each original aside and its translation in; put the originals back if one fails."""
    moved: list[Path] = []
    try:
        for item in patch.files:
            target = folder / item.name
            os.replace(target, backup_path(target))
            moved.append(target)
            os.replace(_temporary_path(target), target)
    except OSError:
        for target in reversed(moved):
            os.replace(backup_path(target), target)
        raise


def restore(folder: Path, patch: Patch, log: Log | None = None) -> list[str]:
    """Put each original back from its ``<name>.bak``; take away what a patch added."""
    _report(log, "正在檢查還原備份。")
    if patch.mount is not None:
        from bokucho.patcher.mount import mount_text, restore_mount

        _report(log, mount_text(patch).removing)
        return restore_mount(folder, patch)
    _report(log, "正在還原原版遊戲檔案。")
    if not inspect(folder, patch).restorable:
        raise PatchError("找不到可以還原的原版備份（.bak）。遊戲檔案沒有被改動。")
    for item in patch.files:
        if (folder / item.name).exists():
            _require_not_in_use(folder / item.name)
    try:
        _restore_title(folder)
        for item in patch.files:
            if _backup_is_original(folder, item):
                target = folder / item.name
                os.replace(backup_path(target), target)
    except PermissionError as error:
        raise _refusal(error, folder) from error
    left = []
    for entry in _recorded(folder):
        path = folder / entry["name"]
        if not path.exists():
            continue
        try:
            if sha256_of(path) == entry["sha256"]:
                path.unlink()
                continue
        except PermissionError:
            pass
        left.append(entry["name"])
    (folder / RECORD).unlink(missing_ok=True)
    return left


def _recipe_engine(patch: Patch) -> str:
    assert patch.recipe is not None
    with ZipFile(io.BytesIO(patch.recipe)) as archive:
        return str(json.loads(archive.read("recipe.json")).get("engine", ""))


def applied_message(patch: Patch, folder: Path | None = None) -> str:
    message = "套用完成，可以開始遊戲了。"
    if patch.mount is not None:
        return message + "\n需要還原時，請再次開啟此補丁並按「還原」。"
    names = [item.name for item in patch.added]
    if patch.recipe is not None and folder is not None:
        names.extend(item["name"] for item in _recorded(folder))
    #: An RGSS game asks for its fill font itself; it has no font menu to visit.
    if patch.recipe is not None and _recipe_engine(patch).startswith("rpgmaker-"):
        names = []
    for name in names:
        if name.lower().endswith(FONT_SUFFIXES):
            family = PurePath(name).stem
            message += (
                f"\n如果以前在遊戲選項裡換過字型，進遊戲後請到「オプション → フォント選択」"
                f"選擇「{family}」。"
            )
    return message


def restored_message(left: list[str]) -> str:
    message = "已經還原成原版。"
    if left:
        message += f"\n這些檔案在套用後被改過，沒有刪除：{'、'.join(left)}。"
    return message
