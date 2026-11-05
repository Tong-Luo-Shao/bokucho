"""Dependency-free contracts for progress, cancellation, and credentials."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock
from typing import Protocol

from bokucho.application.errors import JobCancelled


class ProgressLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: str
    completed: int
    total: int | None
    message_key: str
    level: ProgressLevel = ProgressLevel.INFO
    detail: str | None = None
    metrics: Mapping[str, object] | None = None
    transient: bool = False


class ProgressSink(Protocol):
    def publish(self, event: ProgressEvent) -> None: ...


class CancellationToken(Protocol):
    def add_cancel_callback(self, callback: Callable[[], None]) -> None: ...

    def raise_if_cancelled(self) -> None: ...


class CredentialStore(Protocol):
    def get_all(self, provider: str, endpoint: str | None) -> tuple[str, ...]: ...

    def add(self, provider: str, endpoint: str | None, secret: str) -> None: ...

    def delete(self, provider: str, endpoint: str | None) -> None: ...


@dataclass(frozen=True, slots=True)
class RasterImage:
    data: bytes
    width: int
    height: int
    mode: str
    format: str | None = None
    has_alpha: bool = False
    icc_profile: bytes | None = None
    exif: bytes | None = None
    palette: tuple[tuple[int, int, int, int], ...] | None = None


class RasterCodec(Protocol):
    def load(self, path: Path, *, source: Path | None = None) -> RasterImage: ...

    def decode(self, data: bytes, *, source: Path | None = None) -> RasterImage: ...

    def same_picture(self, first: bytes, second: bytes) -> bool: ...

    def encode(
        self,
        image: RasterImage,
        suffix: str,
        mode: str,
        *,
        reference: RasterImage | None = None,
        source: Path | None = None,
    ) -> bytes: ...


class CancellationFlag:
    """Cancels a job, or pauses it, at the checkpoints it already polls."""

    def __init__(self) -> None:
        self._event = Event()
        self._resumed = Event()
        self._resumed.set()
        self._lock = Lock()
        self._callbacks: list[Callable[[], None]] = []

    def pause(self) -> None:
        self._resumed.clear()

    def resume(self) -> None:
        self._resumed.set()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            self._resumed.set()
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            callback()

    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        with self._lock:
            cancelled = self._event.is_set()
            if not cancelled:
                self._callbacks.append(callback)
        if cancelled:
            callback()

    def raise_if_cancelled(self) -> None:
        self._resumed.wait()
        if self._event.is_set():
            raise JobCancelled()


class NullProgressSink:
    def publish(self, event: ProgressEvent) -> None:
        del event


class NeverCancelled:
    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        del callback

    def raise_if_cancelled(self) -> None:
        return
