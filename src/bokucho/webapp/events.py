"""EventBus — pushes backend events to the frontend via window.evaluate_js."""

from __future__ import annotations

import json
from typing import Protocol


class _Window(Protocol):
    def evaluate_js(self, script: str) -> object: ...


class EventBus:
    """Pushes events to ``bokucho.onEvent`` in the loaded page."""

    def __init__(self, window: _Window | None = None) -> None:
        self._window = window
        self._pending: list[dict] = []
        self._closed = False

    def bind(self, window: _Window) -> None:
        self._window = window
        pending, self._pending = self._pending, []
        for event in pending:
            self._emit(event)

    def close(self) -> None:
        """Stop sending to the page; the window is closing."""
        self._closed = True
        self._pending.clear()

    def push(self, event: dict) -> None:
        if self._closed:
            return
        if self._window is None:
            self._pending.append(event)
            return
        self._emit(event)

    def _emit(self, event: dict) -> None:
        self._window.evaluate_js(f"bokucho.onEvent({json.dumps(event)})")
