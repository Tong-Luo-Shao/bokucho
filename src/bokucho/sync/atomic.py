"""Atomic filesystem writes for user-managed project data."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
import threading
import time
from _thread import RLock
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger(__name__)

_RETRYABLE_WINDOWS_ERRORS = frozenset({5, 32, 33})
_REPLACE_RETRY_DELAYS = (0.02, 0.04, 0.08, 0.16, 0.32)
_DESTINATION_LOCKS: dict[str, RLock] = {}
_DESTINATION_LOCKS_GUARD = threading.Lock()
_WRITTEN_DIGESTS: dict[str, bytes] = {}
_OVERWRITE_OBSERVERS: list[Callable[[Path, str], None]] = []


class OverwriteRefused(OSError):
    """An overwrite observer found a change on disk that the write would undo."""


def _key(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve(strict=False)))


def file_lock(path: str | Path) -> RLock:
    """The lock every write to ``path`` holds; hold it across a read-modify-write too."""
    key = _key(path)
    with _DESTINATION_LOCKS_GUARD:
        return _DESTINATION_LOCKS.setdefault(key, threading.RLock())


def written_here(path: str | Path) -> bool:
    """Whether ``path`` still holds exactly what this process last wrote to it."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    return _WRITTEN_DIGESTS.get(_key(path)) == hashlib.sha256(data).digest()


def observe_overwrites(observer: Callable[[Path, str], None]) -> Callable[[], None]:
    """Show ``observer`` each file and the text about to replace it; return how to stop.

    An observer that raises :class:`OverwriteRefused` stops the write.
    """
    _OVERWRITE_OBSERVERS.append(observer)
    return lambda: _OVERWRITE_OBSERVERS.remove(observer)


def _replace_with_retry(source: Path, destination: Path) -> None:
    for delay in (*_REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if (
                delay is None
                or getattr(error, "winerror", None)
                not in _RETRYABLE_WINDOWS_ERRORS
            ):
                raise
            time.sleep(delay)


def _show_observers(destination: Path, text: str) -> None:
    for observer in tuple(_OVERWRITE_OBSERVERS):
        try:
            observer(destination, text)
        except OverwriteRefused:
            raise
        except Exception:
            _log.exception("Could not check %s before replacing it", destination)


def _write_atomically(
    destination: Path, data: bytes, before_replace: Callable[[], None] = lambda: None
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(destination):
        fd, raw_temp = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temp = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            before_replace()
            _replace_with_retry(temp, destination)
        finally:
            with contextlib.suppress(OSError):
                temp.unlink()


def atomic_write_text(path: str | Path, text: str) -> None:
    """Replace ``path`` only after its UTF-8 contents are fully persisted."""
    destination = Path(path)
    data = text.encode("utf-8")
    with file_lock(destination):
        _write_atomically(destination, data, lambda: _show_observers(destination, text))
        _WRITTEN_DIGESTS[_key(destination)] = hashlib.sha256(data).digest()


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Replace ``path`` only after its binary contents are fully persisted."""
    _write_atomically(Path(path), data)
