"""``EngineAdapter`` — plugin seam #1 (extract / inject)."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from pathlib import Path

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject
from bokucho.model import TextUnit
from bokucho.patcher.core import RECORD, backup_path

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()


def archive_digest(archive: Path) -> str:
    """The SHA-256 an unpacked cache of ``archive`` is stamped with."""
    with archive.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unpacked_stamp(cache: Path) -> Path:
    """The file naming the digest of the archive ``cache`` was unpacked from."""
    return cache.with_name(f"{cache.name}.sha256")


def unpacked_from(cache: Path, digest: str) -> bool:
    """Whether ``cache`` holds the archive whose digest is ``digest``."""
    stamp = unpacked_stamp(cache)
    return cache.is_dir() and stamp.is_file() and stamp.read_text(encoding="ascii") == digest


def _compiled(archive: Path) -> str | None:
    """The digest the patcher recorded for the ``archive`` it compiled, if it recorded one."""
    try:
        record = json.loads((archive.parent / RECORD).read_text(encoding="utf-8"))
        compiled = record.get("compiled", {}).get(archive.name)
    except (OSError, ValueError, AttributeError):
        return None
    return compiled if isinstance(compiled, str) else None


def holds_original(cache: Path, archive: Path, digest: str) -> bool:
    """Whether ``cache`` holds ``archive``, or the original a patch applied to it replaced.

    An archive that may carry a patch is refused unless its original is what
    ``cache`` holds: the translation must never be unpacked as source.
    """
    if unpacked_from(cache, digest):
        return True
    backup = backup_path(archive)
    compiled = _compiled(archive)
    if compiled == digest:
        if backup.is_file() and unpacked_from(cache, archive_digest(backup)):
            return True
        raise InvalidProject(
            f"{archive.name} carries this project's patch, and the original it replaced was "
            "never unpacked here. Restore the original game file first."
        )
    if compiled is None and cache.exists() and backup.is_file():
        if archive_digest(backup) != digest:
            raise InvalidProject(
                f"{archive.name} is not the game this project unpacked, and {backup.name} "
                "beside it is another file. If a patch was applied to this game, restore the "
                f"original first; if the game was updated, move {backup.name} away."
            )
    return False


class NoPixelFont(FileNotFoundError):
    """The game draws its text with a system face: it has no ``.lft`` of its own."""


class EngineAdapter(ABC):
    """Abstract base class for engine adapters."""

    name: str = ""

    @classmethod
    def game_root(cls, game_path: Path) -> Path | None:
        """``game_path`` as a directory of files, or ``None`` to unpack first."""
        return game_path if game_path.is_dir() else None

    @classmethod
    def packed_archive(cls, game_path: Path) -> Path:
        """The file ``game_path`` unpacks from when ``game_root`` is ``None``."""
        return game_path

    @abstractmethod
    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        """Extract translatable text from ``game_path``."""
        raise NotImplementedError

    @abstractmethod
    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        """Write translated ``units`` back into the game."""
        raise NotImplementedError
