from __future__ import annotations

from pathlib import Path

import pytest

from bokucho.application.contracts import CancellationFlag
from bokucho.application.errors import InvalidProject, JobCancelled
from bokucho.application.projects import (
    CreateProjectRequest,
    create_project,
    extract_text,
    inject_translations,
    load_project_summary,
    resolve_game_path,
)
from bokucho.application.text import save_target, set_status
from bokucho.engines.base import EngineAdapter
from bokucho.model import Status, TextUnit
from bokucho.project import Project


class FakeAdapter(EngineAdapter):
    name = "fake"

    def __init__(self) -> None:
        self.injected: list[TextUnit] | None = None
        self.inject_out: Path | None = None

    def extract(self, game_path: Path, sink=None, token=None) -> list[TextUnit]:
        del sink, token
        assert game_path.is_dir()
        return [
            TextUnit(
                id="a:1",
                source="one",
                engine_ref={"lsb": "a.lsb"},
            ),
            TextUnit(
                id="b:1",
                source="two",
                engine_ref={"lsb": "sub/b.lsb"},
            ),
        ]

    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink=None,
        token=None,
    ) -> None:
        del sink, token
        assert game_path.is_dir()
        self.injected = list(units)
        self.inject_out = out_path


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "project"
    created = create_project(
        CreateProjectRequest(
            destination=root,
            name="test",
            source_lang="ja",
            target_lang="zh-Hant",
            engine="livemaker",
        )
    )
    game = root / "game"
    game.mkdir()
    engine = created.config.engine.model_copy(update={"game_path": "game"})
    config = created.config.model_copy(update={"engine": engine})
    created.save_config(config)
    return Project.load(root)


def test_create_project_and_load_summary(tmp_path: Path) -> None:
    root = tmp_path / "created"
    project = create_project(
        CreateProjectRequest(root, "sample", "ja", "zh-Hant", "livemaker")
    )

    summary = load_project_summary(root)

    assert project.root == root.resolve()
    assert summary.root == root.resolve()
    assert summary.name == "sample"
    assert summary.total_units == 0
    assert summary.status_counts[Status.UNTRANSLATED] == 0


def test_create_project_refuses_overwrite(tmp_path: Path) -> None:
    request = CreateProjectRequest(tmp_path, "sample", "ja", "zh-Hant", "livemaker")
    create_project(request)

    with pytest.raises(InvalidProject, match="already exists"):
        create_project(request)


def test_resolve_game_path_rejects_missing_configuration(tmp_path: Path) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path, "sample", "ja", "zh-Hant", "livemaker")
    )

    with pytest.raises(InvalidProject, match="game_path"):
        resolve_game_path(project)


def test_inject_rejects_empty_project(project: Project, tmp_path: Path) -> None:
    with pytest.raises(InvalidProject, match="No text units"):
        inject_translations(project, FakeAdapter(), tmp_path / "out")



def test_cancel_callbacks_run_once_and_late_callbacks_run_immediately() -> None:
    flag = CancellationFlag()
    calls: list[str] = []
    flag.add_cancel_callback(lambda: calls.append("early"))

    flag.cancel()
    flag.cancel()
    flag.add_cancel_callback(lambda: calls.append("late"))

    assert calls == ["early", "late"]
    with pytest.raises(JobCancelled):
        flag.raise_if_cancelled()


class _ScriptAdapter(FakeAdapter):
    """Extracts one script literal with the status the rules give it this time."""

    def __init__(self, status: Status, source: str = "[Normal]") -> None:
        super().__init__()
        self.status = status
        self.source = source

    def extract(self, game_path: Path, sink=None, token=None) -> list[TextUnit]:
        del game_path, sink, token
        return [
            TextUnit(
                id="Scripts:s0:1:7",
                source=self.source,
                status=self.status,
                kind="script",
                engine_ref={"file": "Scripts", "address": "s0:1:7", "kind": "script"},
            )
        ]


def test_a_literal_the_translator_opted_in_stays_opted_in_after_extracting_again(
    project: Project,
) -> None:
    extract_text(project, _ScriptAdapter(Status.IGNORED))
    set_status(project, "Scripts", "Scripts:s0:1:7", Status.UNTRANSLATED)
    extract_text(project, _ScriptAdapter(Status.IGNORED))
    [strings] = project.load_string_files()
    assert strings.by_id("Scripts:s0:1:7").status is Status.UNTRANSLATED


def test_a_literal_nobody_touched_follows_the_rules_again(project: Project) -> None:
    extract_text(project, _ScriptAdapter(Status.UNTRANSLATED))
    extract_text(project, _ScriptAdapter(Status.IGNORED))
    [strings] = project.load_string_files()
    assert strings.by_id("Scripts:s0:1:7").status is Status.IGNORED


def test_a_script_address_holding_another_literal_starts_over_and_keeps_the_old_target(
    project: Project,
) -> None:
    extract_text(project, _ScriptAdapter(Status.UNTRANSLATED, "To Title"))
    save_target(project, "Scripts", "Scripts:s0:1:7", "回到標題")
    extract_text(project, _ScriptAdapter(Status.IGNORED, "Miss"))
    [strings] = project.load_string_files()
    unit = strings.by_id("Scripts:s0:1:7")
    assert (unit.source, unit.status, unit.target) == ("Miss", Status.IGNORED, "")
    assert [entry.target for entry in unit.history] == ["回到標題"]
