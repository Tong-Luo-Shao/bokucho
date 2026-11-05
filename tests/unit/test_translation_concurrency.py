"""A key can keep more than one request in the air when its interface says so."""

from __future__ import annotations

import threading
import time

from bokucho.application.text import translate_project
from bokucho.application.translation_limits import TranslationLane
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project, TranslatorConfig
from bokucho.translators.base import Translator

UNITS = 6


class _Recorder(Translator):
    name = "recorder"

    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def translate(self, batch, context, glossary=None):
        del context, glossary
        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            time.sleep(0.05)
            return [f"譯:{unit.source}" for unit in batch]
        finally:
            with self.lock:
                self.in_flight -= 1


def _project(new_project, concurrency: int) -> Project:
    project = new_project("Concurrency", source_lang="und")
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[TextUnit(id=f"u{index}", source=f"元{index}") for index in range(UNITS)],
        ),
        "a.lsb.json",
    )
    base = project.config.translator
    interface = base.selected_interface().model_copy(
        update={
            "batch_size": 1,
            "max_request_tokens": 0,
            "context_units": 0,
            "concurrency": concurrency,
        }
    )
    translator = TranslatorConfig.model_validate(
        {
            **base.model_dump(),
            "interfaces": [interface.model_dump()],
            "active_interface_id": interface.id,
        }
    )
    project.save_config(project.config.model_copy(update={"translator": translator}))
    return Project.load(project.root)


def _translate(project: Project, lanes: int = 1) -> _Recorder:
    recorder = _Recorder()
    translate_project(
        project,
        lanes=tuple(TranslationLane(recorder) for _ in range(lanes)),
        sleep=lambda _: None,
    )
    return recorder


def _statuses(project: Project) -> list[Status]:
    return [unit.status for file in project.load_string_files() for unit in file.units]


def test_one_key_keeps_as_many_requests_in_flight_as_its_concurrency(new_project) -> None:
    project = _project(new_project, concurrency=3)

    recorder = _translate(project)

    assert 1 < recorder.peak <= 3
    assert _statuses(project) == [Status.MT] * UNITS


def test_concurrency_multiplies_across_keys(new_project) -> None:
    project = _project(new_project, concurrency=2)

    recorder = _translate(project, lanes=2)

    assert 2 < recorder.peak <= 4
    assert _statuses(project) == [Status.MT] * UNITS


def test_a_run_reports_the_requests_it_sent_at_once_not_the_ceiling(new_project) -> None:
    project = _project(new_project, concurrency=10)

    result = translate_project(
        project, lanes=(TranslationLane(_Recorder()),), sleep=lambda _: None
    )

    assert result.lanes == UNITS


def test_a_concurrency_of_one_sends_one_request_per_key_at_a_time(new_project) -> None:
    project = _project(new_project, concurrency=1)

    recorder = _translate(project)

    assert recorder.peak == 1
    assert _statuses(project) == [Status.MT] * UNITS
