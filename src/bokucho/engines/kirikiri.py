"""KiriKiri / KAG games: plain ``.xp3`` archives holding ``.ks`` scenarios.

The game finds a storage by file name alone, and the archive it loaded last wins, so the
copy of each ``.ks`` that counts is the one in the last loaded archive that holds it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject
from bokucho.engines import kag, xp3
from bokucho.engines.base import EngineAdapter
from bokucho.model import Context, TextUnit
from bokucho.patcher.payload import safe_parts

NAMES_FILE = "speakers"
DATA = "data.xp3"
CATEGORIES = {
    2: "scenarios compiled to .scn",
    3: "encrypted file contents",
    4: "a publisher's own archive index",
}
_STARTUP_FIRST = "system/initialize.tjs"
_USE_ARCHIVE = re.compile(r"""useArchiveIfExists\(\s*(["'])([^"'\r\n]+)\1\s*\)""")
_PATCH_LOOP = re.compile(r"""(["'])patch\1\s*\+\s*[A-Za-z_]\w*\s*\+\s*(["'])\.xp3""", re.I)
_PATCH = re.compile(r"patch([1-9][0-9]*)?\.xp3", re.I)
_IMAGE_MAGIC = {
    ".png": b"\x89PNG",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
    ".bmp": b"BM",
    ".tlg": b"TLG",
}
_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()


class UnsupportedCategory(InvalidProject):
    """A KiriKiri game of a kind this version cannot translate yet."""

    def __init__(self, category: int, where: str) -> None:
        super().__init__(
            f"{where}: KiriKiri category {category} ({CATEGORIES[category]}) is not supported yet."
        )
        self.category = category


class SlotRefused(ValueError):
    """No patch archive name the game would load last is free (message for the player)."""


@dataclass(frozen=True, slots=True)
class GameLayout:
    """The archives the game loads, in load order, and how it loads patch archives."""

    folder: Path
    archives: tuple[str, ...]
    slots: str  # "loop", "single", or "" when the game loads no patch archive


class ScriptSource(NamedTuple):
    archive: str
    entry: xp3.Entry


def _find(folder: Path, relative: str) -> str | None:
    """``relative`` as the folder spells it, matched in any case; ``None`` when missing."""
    current = folder
    parts: list[str] = []
    for part in relative.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        try:
            names = sorted(child.name for child in current.iterdir())
        except OSError:
            return None
        if part not in names:
            part = next((name for name in names if name.casefold() == part.casefold()), "")
            if not part:
                return None
        parts.append(part)
        current = current / part
    return "/".join(parts) if parts and current.is_file() else None


def _entries(folder: Path, archive: str) -> list[xp3.Entry]:
    try:
        return xp3.entries(folder / archive)
    except xp3.CustomIndex as error:
        raise UnsupportedCategory(4, archive) from error
    except (xp3.NotAnArchive, OSError) as error:
        raise InvalidProject(f"{archive} cannot be read: {error}") from error


def _read(folder: Path, archive: str, entry: xp3.Entry) -> bytes:
    try:
        return xp3.read(folder / archive, entry)
    except (xp3.NotAnArchive, OSError) as error:
        raise InvalidProject(f"{archive} cannot be read: {error}") from error


def _text(folder: Path, archive: str, entry: xp3.Entry) -> str:
    try:
        return kag.decode_script(_read(folder, archive, entry))
    except kag.ScrambledText as error:
        raise UnsupportedCategory(3, f"{archive}: {entry.name}") from error


def _startup(folder: Path, data: str) -> tuple[list[str], bool]:
    """The archives the startup script loads, and whether it runs the patch loop."""
    scripts = sorted(
        (entry for entry in _entries(folder, data) if entry.name.lower().endswith(".tjs")),
        key=lambda entry: entry.name.lower() != _STARTUP_FIRST,
    )
    for entry in scripts:
        text = _text(folder, data, entry)
        names = [match.group(2) for match in _USE_ARCHIVE.finditer(text)]
        if names:
            return names, bool(_PATCH_LOOP.search(text))
    return [], False


def game_layout(folder: Path) -> GameLayout:
    """Which archives the game in ``folder`` loads, in the order it loads them."""
    data = _find(folder, DATA)
    if data is None:
        raise InvalidProject(f"{folder}: there is no {DATA}, so this is not a KiriKiri game.")
    names, loop = _startup(folder, data)
    archives = [data]
    for name in names:
        found = _find(folder, name)
        if found is not None and found.casefold() not in {a.casefold() for a in archives}:
            archives.append(found)
    slots = ""
    if any(
        PurePosixPath(name.replace("\\", "/")).as_posix().lower() == "patch.xp3" for name in names
    ):
        slots = "loop" if loop else "single"
    if slots == "loop":
        number = 2
        while (found := _find(folder, f"patch{number}.xp3")) is not None:
            if found.casefold() not in {a.casefold() for a in archives}:
                archives.append(found)
            number += 1
    return GameLayout(folder, tuple(archives), slots)


def patch_slot(folder: Path, slots: str) -> str:
    """The patch archive name the game in ``folder`` would load last, for ``slots``."""
    present: dict[int, str] = {}
    for child in sorted(folder.iterdir()):
        match = _PATCH.fullmatch(child.name)
        if match and child.is_file():
            present.setdefault(int(match.group(1) or 0), child.name)
    if slots == "single":
        if 0 in present:
            raise SlotRefused(
                f"遊戲資料夾裡已經有 {present[0]}。這款遊戲只會載入一個補丁封包，"
                "所以無法再加入翻譯補丁。"
            )
        return "patch.xp3"
    if slots != "loop":
        raise SlotRefused("這款遊戲不會載入補丁封包（patch.xp3），所以無法套用翻譯補丁。")
    numbered = {number: name for number, name in present.items() if number >= 2}
    if 0 not in present and not numbered:
        return "patch.xp3"
    number = 2
    while number in numbered:
        number += 1
    later = sorted(n for n in numbered if n > number)
    if later:
        raise SlotRefused(
            f"遊戲資料夾裡有 {numbered[later[0]]}，但前面缺少 patch{number}.xp3，"
            "所以遊戲目前不會載入它。加入翻譯補丁會讓遊戲開始載入它，因此不套用。"
        )
    return f"patch{number}.xp3"


def scripts(folder: Path, archives: Sequence[str]) -> dict[str, ScriptSource]:
    """Each ``.ks`` the game reads, by basename, from the last archive that holds it."""
    chosen: dict[str, tuple[str, ScriptSource]] = {}
    for archive in archives:
        here: dict[str, str] = {}
        for entry in _entries(folder, archive):
            if not entry.name.lower().endswith(".ks"):
                continue
            base = entry.name.rsplit("/", 1)[-1]
            try:
                safe_parts(base)
            except ValueError:
                raise InvalidProject(
                    f"{archive}: {entry.name} is not a plain file name, so its translation "
                    "cannot be written out; this layout is not supported."
                ) from None
            key = base.casefold()
            if key in here:
                raise InvalidProject(
                    f"{archive}: {here[key]} and {entry.name} share one file name, and the "
                    "game finds scenarios by name alone; this layout is not supported."
                )
            here[key] = entry.name
            chosen[key] = (base, ScriptSource(archive, entry))
    return dict(chosen.values())


def _check_images(folder: Path, archive: str) -> None:
    for entry in _entries(folder, archive):
        magic = _IMAGE_MAGIC.get(PurePosixPath(entry.name).suffix.lower())
        if magic is not None:
            if not _read(folder, archive, entry).startswith(magic):
                raise UnsupportedCategory(3, f"{archive}: {entry.name}")
            return


class KiriKiriAdapter(EngineAdapter):
    """Extract and inject the text of ``.ks`` scenarios in plain ``.xp3`` archives."""

    name = "kirikiri"

    def __init__(
        self,
        skip_branches: Sequence[str] = (),
        name_attributes: Sequence[str] = (),
        archives: Sequence[str] | None = None,
        skip_labels: Sequence[str] = (),
    ) -> None:
        self.rules = kag.KagRules.from_config(skip_branches, name_attributes, skip_labels)
        self.archives = None if archives is None else tuple(archives)

    @classmethod
    def game_root(cls, game_path: Path) -> Path | None:
        return game_path if game_path.is_dir() else game_path.parent

    def _archives(self, folder: Path) -> tuple[str, ...]:
        if self.archives is None:
            return game_layout(folder).archives
        found = []
        for name in self.archives:
            spelled = _find(folder, name)
            if spelled is None:
                raise InvalidProject(f"{folder}: {name} is missing; the patch was made with it.")
            found.append(spelled)
        return tuple(found)

    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        folder = self.game_root(game_path)
        archives = self._archives(folder)
        found = scripts(folder, archives)
        if not found:
            if any(
                entry.name.lower().endswith(".scn")
                for archive in archives
                for entry in _entries(folder, archive)
            ):
                raise UnsupportedCategory(2, str(folder))
            raise InvalidProject(f"{folder}: the game's archives hold no .ks scenarios.")
        _check_images(folder, archives[0])
        lines: list[TextUnit] = []
        names: dict[str, TextUnit] = {}
        for base in sorted(found):
            token.raise_if_cancelled()
            text = _text(folder, *found[base])
            for piece in kag.pieces(text, base, self.rules):
                if piece.kind == "name":
                    names.setdefault(
                        piece.id,
                        TextUnit(
                            id=piece.id,
                            source=piece.source,
                            kind="name",
                            engine_ref={"file": NAMES_FILE},
                        ),
                    )
                else:
                    lines.append(
                        TextUnit(
                            id=piece.id,
                            source=piece.source,
                            kind="dialogue",
                            context=Context(speaker=piece.speaker),
                            engine_ref={"file": base},
                        )
                    )
        return lines + list(names.values())

    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        folder = self.game_root(game_path)
        found = scripts(folder, self._archives(folder))
        targets = {unit.id: unit.target for unit in units if unit.target.strip()}
        out_path.mkdir(parents=True, exist_ok=True)
        for base in sorted(found):
            token.raise_if_cancelled()
            text = _text(folder, *found[base])
            try:
                new = kag.rewrite(text, base, self.rules, targets)
            except kag.UnsafeTarget as error:
                raise InvalidProject(str(error)) from error
            if new != text:
                (out_path / base).write_bytes(kag.encode_script(new))
