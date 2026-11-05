"""Thread-based job runner and event delivery for the web application."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from enum import StrEnum
from threading import Thread
from time import monotonic
from typing import Any

from bokucho.application.contracts import (
    CancellationFlag,
    CancellationToken,
    ProgressEvent,
    ProgressSink,
)
from bokucho.application.errors import AppError, JobBusy, JobCancelled
from bokucho.crash_logging import CrashLogger
from bokucho.webapp.events import EventBus


class JobState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"


class BusProgressSink:
    """Publishes progress without flooding the webview's JavaScript bridge."""

    def __init__(self, events: EventBus, job: str) -> None:
        self._events = events
        self._job = job
        self._last_emitted_at: float | None = None
        self._last_phase: str | None = None

    def publish(self, event: ProgressEvent) -> None:
        if event.transient:
            self._push(event)
            return
        now = monotonic()
        phase_changed = event.phase != self._last_phase
        finished = event.total is not None and event.completed >= event.total
        if (
            not phase_changed
            and not finished
            and self._last_emitted_at is not None
            and now - self._last_emitted_at < 0.05
        ):
            return
        self._last_emitted_at = now
        self._last_phase = event.phase
        self._push(event)

    def _push(self, event: ProgressEvent) -> None:
        self._events.push(
            {
                "type": "progress",
                "job": self._job,
                "phase": event.phase,
                "current": event.completed,
                "total": event.total,
                "message": event.message_key,
                "detail": event.detail,
                "metrics": event.metrics,
                "transient": event.transient,
            }
        )


class LogRelay(logging.Handler):
    """Sends what a running job logs to the job bar's details panel."""

    LOGGER = "bokucho"

    def __init__(self, events: EventBus, job: str, redact: Callable[[str], str]) -> None:
        super().__init__(level=logging.INFO)
        self._events = events
        self._job = job
        self._redact = redact

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(record, "ui_hidden", False):
                return
            message = record.getMessage()
            if record.levelno > logging.INFO:
                message = f"{record.levelname} {message}"
            self._events.push(
                {
                    "type": "log",
                    "job": self._job,
                    "message": self._redact(message),
                }
            )
        except Exception:  # noqa: BLE001 - a handler may never raise into its caller
            self.handleError(record)

    def __enter__(self) -> LogRelay:
        logging.getLogger(self.LOGGER).addHandler(self)
        return self

    def __exit__(self, *exc_info: object) -> None:
        logging.getLogger(self.LOGGER).removeHandler(self)


class JobRunner:
    """Single-flight background job runner for the webapp bridge."""

    def __init__(self, events: EventBus, crash_logger: CrashLogger | None = None) -> None:
        self._events = events
        self._crash_logger = crash_logger or CrashLogger()
        self._state = JobState.IDLE
        self._token: CancellationFlag | None = None
        self._thread: Thread | None = None
        self._active_name: str | None = None

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def active_name(self) -> str | None:
        return self._active_name

    def start(
        self,
        name: str,
        operation: Callable[[ProgressSink, CancellationToken], Any],
        mutating: bool = True,
    ) -> None:
        if mutating and self._state is not JobState.IDLE:
            raise JobBusy(f"Another operation is already running: {self._active_name}")
        self._crash_logger.prepare()
        token = CancellationFlag()
        self._token = token
        self._active_name = name
        self._state = JobState.RUNNING
        thread = Thread(target=self._run, args=(name, operation, token), daemon=True)
        self._thread = thread
        thread.start()

    def cancel(self) -> bool:
        if self._state not in (JobState.RUNNING, JobState.PAUSED) or self._token is None:
            return False
        self._state = JobState.CANCELLING
        self._token.cancel()
        return True

    def pause(self) -> bool:
        if self._state is not JobState.RUNNING or self._token is None:
            return False
        self._state = JobState.PAUSED
        self._token.pause()
        self._push_terminal("paused", self._active_name or "")
        return True

    def resume(self) -> bool:
        if self._state is not JobState.PAUSED or self._token is None:
            return False
        self._state = JobState.RUNNING
        self._token.resume()
        self._push_terminal("resumed", self._active_name or "")
        return True

    def register_secrets(self, secrets: Iterable[str]) -> None:
        self._crash_logger.register(secrets)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(
        self,
        name: str,
        operation: Callable[[ProgressSink, CancellationToken], Any],
        token: CancellationFlag,
    ) -> None:
        sink = BusProgressSink(self._events, name)
        relay = LogRelay(self._events, name, self._crash_logger.redactor.redact)
        try:
            with relay:
                value = operation(sink, token)
            token.raise_if_cancelled()
        except JobCancelled:
            self._finish()
            self._push_terminal("cancelled", name)
        except AppError as error:
            self._finish()
            redacted = self._crash_logger.redactor.redact(str(error))
            self._push_terminal("failed", name, message=redacted)
        except Exception as error:  # noqa: BLE001 - sanitize at the worker boundary
            try:
                report = self._crash_logger.capture(error)
            except Exception:  # noqa: BLE001 - the terminal event must always be pushed
                message = (
                    f"Unexpected job failure ({type(error).__name__}); "
                    "diagnostic unavailable"
                )
            else:
                message = (
                    f"Unexpected job failure ({type(error).__name__}); "
                    f"diagnostic: {report.path.name}"
                )
            self._finish()
            self._push_terminal("failed", name, message=message)
        else:
            self._finish()
            self._push_terminal("succeeded", name, detail=value)

    def _push_terminal(
        self,
        event_type: str,
        name: str,
        message: str | None = None,
        detail: object = None,
    ) -> None:
        self._events.push(
            {
                "type": event_type,
                "job": name,
                "current": None,
                "total": None,
                "message": message,
                "detail": detail,
            }
        )

    def _finish(self) -> None:
        self._token = None
        self._active_name = None
        self._state = JobState.IDLE
