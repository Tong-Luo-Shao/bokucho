"""The values a LiveMaker game derives, checked on the quality screen and fixed from it."""

from __future__ import annotations

import json
import time
from pathlib import Path

from bokucho.application.quality import fix_values
from bokucho.findings import Finding
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.webapp.api import Api
from bokucho.webapp.events import EventBus
from bokucho.webapp.state import AppState

from .conftest import FakeWindow

_PREFIX = "bokucho.onEvent("
UNIT = "pylm:value:0000000000000001"


class ValuesAdapter:
    """Stands in for the LiveMaker adapter: one value whose draft "bad" breaks a check."""

    def __init__(self) -> None:
        self.analysed = 0

    def analyse_values(self, game_path, token=None):
        self.analysed += 1
        return "the analysis"

    @staticmethod
    def value_problems(values, units):
        broken = [unit for unit in units if unit.id == UNIT and unit.target == "bad"]
        finding = Finding(
            "values",
            "values_join",
            {"where": "a.lsb:1", "pieces": "'a' ++ 'b'", "whole": "'ab'", "game": "'a'"},
        )
        return [(UNIT, finding)] if broken else []

    @staticmethod
    def assemble_values(values, units):
        return {UNIT: "good"}


def _project(new_project, tmp_path: Path) -> Project:
    project = new_project("values", root=tmp_path / "values")
    (project.root / "Game.exe").write_bytes(b"MZ")
    project.save_config(
        project.config.model_copy(
            update={"engine": project.config.engine.model_copy(update={"game_path": "Game.exe"})}
        )
    )
    project.save_string_file(
        StringFile(
            source_file="values",
            units=[TextUnit(id=UNIT, source="ab", target="bad", status=Status.MT, kind="value")],
        ),
        "values.json",
    )
    return Project.load(project.root)


def _wait_for_values(window: FakeWindow, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pushed = [json.loads(call[len(_PREFIX) : -1]) for call in window.calls]
        if any(event["type"] == "values-analysed" for event in pushed):
            return
        time.sleep(0.02)
    raise AssertionError("The game's values were never read")


def test_a_derived_value_that_no_longer_matches_is_a_finding_once_the_game_is_read(
    new_project, tmp_path: Path, app_state: AppState, fake_window: FakeWindow
) -> None:
    adapter = ValuesAdapter()
    api = Api(app_state, EventBus(fake_window), adapter_factory=lambda project: adapter)
    assert "error" not in api.open_project(str(_project(new_project, tmp_path).root))

    first = api.quality()
    assert not [issue for issue in first["issues"] if issue["check"] == "values"]
    _wait_for_values(fake_window)
    report = api.quality()
    api.quality()

    (finding,) = [issue for issue in report["issues"] if issue["check"] == "values"]
    assert (finding["file"], finding["unit_id"]) == ("values", UNIT)
    assert report["passed"] is False
    assert api.values_preview() == {"fixed": 1}
    # Read once: what it finds changes only with the game.
    assert adapter.analysed == 1


def test_fixing_values_writes_the_assembled_drafts_and_keeps_their_status(
    new_project, tmp_path: Path
) -> None:
    project = _project(new_project, tmp_path)

    result = fix_values(project, lambda units: ValuesAdapter.assemble_values(None, units))

    (unit,) = Project.load(project.root).load_string_files()[0].units
    assert result.fixed == 1
    assert (unit.target, unit.status) == ("good", Status.MT)
