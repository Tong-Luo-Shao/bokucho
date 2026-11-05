"""Tests for the thread job runner."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event

import pytest

from bokucho.application.contracts import ProgressEvent
from bokucho.application.errors import AppError, JobCancelled
from bokucho.crash_logging import CrashLogger
from bokucho.webapp.api import Api
from bokucho.webapp.jobs import BusProgressSink, JobRunner, JobState
from bokucho.webapp.registry import JOBS

from .conftest import FakeWindow

_PREFIX = "bokucho.onEvent("


def _pushed_events(fake_window: FakeWindow) -> list[dict]:
    return [json.loads(call[len(_PREFIX) : -1]) for call in fake_window.calls]


def test_start_job_pushes_ordered_progress_then_succeeded(
    api: Api,
    fake_window: FakeWindow,
) -> None:
    def operation(sink, token):
        sink.publish(ProgressEvent("translate", 1, 2, "translate.progress"))
        sink.publish(ProgressEvent("translate", 2, 2, "translate.progress"))
        return {"translated": 2}

    api.registry["translate"] = lambda state: operation

    result = api.start_job("translate")
    assert "error" not in result

    api.jobs.join(timeout=2)
    assert api.jobs.state is JobState.IDLE

    events = _pushed_events(fake_window)
    assert [event["type"] for event in events] == ["progress", "progress", "succeeded"]
    assert [event["current"] for event in events[:2]] == [1, 2]
    assert [event["phase"] for event in events[:2]] == ["translate", "translate"]
    assert events[-1]["detail"] == {"translated": 2}


def test_second_mutating_job_is_rejected_while_one_runs(api: Api) -> None:
    started = Event()
    release = Event()

    def blocking(sink, token):
        started.set()
        release.wait(2)
        return None

    api.registry["translate"] = lambda state: blocking
    api.registry["extract"] = lambda state: (lambda sink, token: None)

    result = api.start_job("translate")
    assert "error" not in result
    assert started.wait(2)

    busy = api.start_job("extract")

    release.set()
    api.jobs.join(timeout=2)

    assert busy["error"]["kind"] == "job_busy"
    assert "running" in busy["error"]["message"].casefold()


def test_cancel_job_pushes_cancelled_and_returns_idle(api: Api, fake_window: FakeWindow) -> None:
    started = Event()
    stop_polling = Event()

    def cancellable(sink, token):
        started.set()
        while not stop_polling.wait(timeout=0.02):
            token.raise_if_cancelled()

    api.registry["translate"] = lambda state: cancellable

    result = api.start_job("translate")
    assert "error" not in result
    assert started.wait(2)

    cancel_result = api.cancel_job()
    assert cancel_result == {"status": "cancelling"}

    api.jobs.join(timeout=2)
    stop_polling.set()

    events = _pushed_events(fake_window)
    assert events[-1]["type"] == "cancelled"
    assert api.jobs.state is JobState.IDLE


def test_cancel_job_reports_idle_when_no_job_is_running(api: Api) -> None:
    assert api.cancel_job() == {"status": "idle"}


def _checkpointed(started: Event, go: Event, reached: list[bool]):
    def operation(sink, token):
        started.set()
        go.wait(2)
        token.raise_if_cancelled()
        reached.append(True)

    return operation


def test_a_paused_job_waits_at_its_checkpoint_until_resumed(
    api: Api, fake_window: FakeWindow
) -> None:
    started, go, reached = Event(), Event(), []
    api.registry["translate"] = lambda state: _checkpointed(started, go, reached)
    api.start_job("translate")
    assert started.wait(2)

    assert api.pause_job() == {"status": "paused"}
    go.set()
    api.jobs.join(timeout=0.2)
    assert reached == []
    assert api.jobs.state is JobState.PAUSED

    assert api.resume_job() == {"status": "running"}
    api.jobs.join(timeout=2)

    assert reached == [True]
    assert [event["type"] for event in _pushed_events(fake_window)] == [
        "paused",
        "resumed",
        "succeeded",
    ]


def test_cancelling_a_paused_job_ends_it(api: Api, fake_window: FakeWindow) -> None:
    started, go, reached = Event(), Event(), []
    api.registry["translate"] = lambda state: _checkpointed(started, go, reached)
    api.start_job("translate")
    assert started.wait(2)
    api.pause_job()
    go.set()

    assert api.cancel_job() == {"status": "cancelling"}
    api.jobs.join(timeout=2)

    assert reached == []
    assert api.jobs.state is JobState.IDLE
    assert _pushed_events(fake_window)[-1]["type"] == "cancelled"


def test_pause_and_resume_report_idle_when_no_job_is_running(api: Api) -> None:
    assert api.pause_job() == {"status": "idle"}
    assert api.resume_job() == {"status": "idle"}


def test_progress_sink_coalesces_dense_updates_but_keeps_completion(
    event_bus,
    fake_window: FakeWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter((10.0, 10.01, 10.02))
    monkeypatch.setattr("bokucho.webapp.jobs.monotonic", lambda: next(timestamps))
    sink = BusProgressSink(event_bus, "images-scan")

    for completed in (1, 2, 3):
        sink.publish(ProgressEvent("images.scan", completed, 3, "scan.item"))

    events = _pushed_events(fake_window)
    assert [event["current"] for event in events] == [1, 3]


def test_a_reply_in_flight_does_not_hold_back_the_next_progress(
    event_bus,
    fake_window: FakeWindow,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter((10.0, 10.06, 10.07))
    monkeypatch.setattr("bokucho.webapp.jobs.monotonic", lambda: next(timestamps))
    sink = BusProgressSink(event_bus, "translate")

    sink.publish(ProgressEvent("translate", 0, 3, "translate.started"))
    sink.publish(ProgressEvent("translate", 0, 3, "translate.streaming", transient=True))
    sink.publish(ProgressEvent("translate", 1, 3, "translate.batch.completed"))

    events = _pushed_events(fake_window)
    assert [event["message"] for event in events] == [
        "translate.started",
        "translate.streaming",
        "translate.batch.completed",
    ]
    assert events[1]["transient"] is True


def test_cancel_job_interrupts_ocr_runtime_loading(
    api: Api,
    fake_window: FakeWindow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = api.create_project(
        name="ocr-cancel",
        engine="livemaker",
        destination=str(tmp_path / "project"),
    )
    assert "error" not in created
    assert "error" not in api.open_project(created["root"])
    started = Event()
    cancelled = Event()

    class BlockingReader:
        @property
        def runtime_report(self) -> dict[str, object]:
            started.set()
            cancelled.wait(timeout=2)
            raise JobCancelled()

        def cancel(self) -> None:
            cancelled.set()

        def close(self) -> None:
            return None

    reader = BlockingReader()
    monkeypatch.setattr(
        "bokucho.webapp.registry.images_mod.get_ocr",
        lambda **kwargs: reader,
    )
    api.registry["images-ocr"] = JOBS["images-ocr"]

    assert api.start_job("images-ocr") == {
        "status": "started",
        "job": "images-ocr",
    }
    assert started.wait(timeout=2)

    assert api.cancel_job() == {"status": "cancelling"}
    api.jobs.join(timeout=2)

    assert cancelled.is_set()
    assert api.jobs.state is JobState.IDLE
    assert _pushed_events(fake_window)[-1]["type"] == "cancelled"


def test_job_failure_redacts_registered_secret_from_event_and_crash_log(
    tmp_path: Path, app_state, event_bus, fake_window: FakeWindow
) -> None:
    secret = "sk-test-123"
    log_dir = tmp_path / "logs"
    runner = JobRunner(event_bus, CrashLogger(log_dir))
    runner.register_secrets((secret,))

    def failing(sink, token):
        raise RuntimeError(f"provider rejected {secret}")

    registry = {"translate": lambda state: failing}
    api = Api(app_state, event_bus, job_runner=runner, registry=registry)

    result = api.start_job("translate")
    assert "error" not in result

    api.jobs.join(timeout=2)

    events = _pushed_events(fake_window)
    failed = [event for event in events if event["type"] == "failed"]
    assert failed
    assert secret not in json.dumps(failed[-1])

    crash_files = list(log_dir.glob("crash-*.log"))
    assert crash_files
    assert secret not in crash_files[0].read_text(encoding="utf-8")


def test_app_error_failure_redacts_registered_secret_from_event(
    api: Api, fake_window: FakeWindow
) -> None:
    secret = "sk-test-789"
    api.jobs.register_secrets((secret,))

    def failing(sink, token):
        raise AppError(f"provider rejected {secret}")

    api.registry["translate"] = lambda state: failing

    result = api.start_job("translate")
    assert "error" not in result

    api.jobs.join(timeout=2)

    events = _pushed_events(fake_window)
    failed = [event for event in events if event["type"] == "failed"]
    assert failed
    assert secret not in json.dumps(failed[-1])
    assert "[REDACTED]" in failed[-1]["message"]


def test_opening_another_project_waits_for_the_running_job(api: Api, tmp_path: Path) -> None:
    roots = [
        api.create_project(name=name, engine="livemaker", destination=str(tmp_path / name))["root"]
        for name in ("first", "second")
    ]
    api.open_project(roots[0])
    started = Event()
    release = Event()

    def blocking(sink, token):
        started.set()
        release.wait(2)

    api.registry["translate"] = lambda state: blocking
    api.start_job("translate")
    assert started.wait(timeout=2)

    refused = api.open_project(roots[1])
    release.set()
    api.jobs.join(timeout=2)

    assert refused["error"]["kind"] == "job_busy"
    assert Path(api.state.project.root) == Path(roots[0])
