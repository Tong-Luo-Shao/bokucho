from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from bokucho.application.contracts import CancellationFlag, ProgressEvent
from bokucho.application.errors import InvalidProject, JobCancelled, ReleaseBlocked
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.quality import run_qa
from bokucho.application.release import build_patch, patcher_stub, release_patcher
from bokucho.engines.base import EngineAdapter, NoPixelFont
from bokucho.exe_icon import exe_icon
from bokucho.model import Status, StringFile, TextUnit
from bokucho.patcher.payload import read_patch
from bokucho.project import FontSourceConfig, Project

pytestmark = pytest.mark.usefixtures("no_rehearsal")

only_on_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows' own resource writer draws the icon"
)
GAME = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"


class FakeAdapter(EngineAdapter):
    name = "fake"

    def extract(self, game_path, sink=None, token=None):
        del game_path, sink, token
        return []

    def inject(self, units, game_path, out_path, sink=None, token=None):
        raise AssertionError("a LiveMaker patch is compiled on the player's machine")


class RepackingAdapter(FakeAdapter):
    def pixel_font(self, game_path):
        return game_path.parent / "font.lft"


class SystemFaceAdapter(FakeAdapter):
    def pixel_font(self, game_path):
        raise NoPixelFont(f"{game_path.name} draws with a system face")


class InconsistentAdapter(RepackingAdapter):
    def check_values(self, units, game_path):
        raise ValueError("Values the game derives from one another would no longer match")


def _repacking(project: Project) -> EngineAdapter:
    return RepackingAdapter()


class CancellingSink:
    """Cancels the release once its executable is written, before it is published."""

    def __init__(self, flag: CancellationFlag) -> None:
        self.flag = flag

    def publish(self, event: ProgressEvent) -> None:
        if event.message_key == "release.patcher.completed":
            self.flag.cancel()


def release_project_fixture(
    tmp_path: Path, unit: TextUnit, game: bytes = b"MZ original game" * 512
) -> tuple[Project, Path]:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "release", "ja", "zh-Hant", "livemaker")
    )
    (project.root / "Game.exe").write_bytes(game)
    (project.root / "font.ttf").write_bytes(b"font source")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": "Game.exe"}),
                "font": project.config.font.model_copy(
                    update={"sources": [FontSourceConfig(path="font.ttf")]}
                ),
            }
        )
    )
    project.save_string_file(
        StringFile(source_file="a.lsb", units=[unit]),
        "a.lsb.json",
    )
    stub = tmp_path / "bokucho-patcher.exe"
    stub.write_bytes(b"MZ the patcher program")
    return Project.load(project.root), stub


def test_release_publishes_timestamped_artifact_after_success(tmp_path: Path) -> None:
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(id="u1", source="hello", target="translated", status=Status.REVIEWED),
    )
    artifacts = tmp_path / "artifacts"

    result = release_patcher(project, lambda current: RepackingAdapter(), artifacts, stub)

    assert result.artifact_dir.parent == artifacts
    assert list(result.artifact_dir.iterdir()) == [result.executable]
    assert result.executable.name == "release-ja2zh-tw-patcher.exe"
    assert read_patch(result.executable.read_bytes()).title == "release-ja2zh-tw-patcher"
    assert not list(artifacts.glob(".bokucho-*"))


def test_a_patch_the_players_patcher_would_refuse_is_not_published(tmp_path: Path) -> None:
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(id="u1", source="hello", target="translated", status=Status.REVIEWED),
    )
    artifacts = tmp_path / "artifacts"

    with pytest.raises(ReleaseBlocked, match="would no longer match"):
        release_patcher(project, lambda current: InconsistentAdapter(), artifacts, stub)

    assert not list(artifacts.rglob("*.exe"))


def test_windows_own_winmm_dll_is_not_taken_for_uif(tmp_path: Path) -> None:
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(id="u1", source="hello", target="translated", status=Status.REVIEWED),
    )
    windows = tmp_path / "System32" / "winmm.dll"
    windows.parent.mkdir()
    windows.write_bytes(b"MZ" + bytes(58) + (64).to_bytes(4, "little") + b"PE\0\0\x4c\x01")
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"uif_dll": str(windows)})
            }
        )
    )

    with pytest.raises(ReleaseBlocked, match="not UIF"):
        release_patcher(
            Project.load(project.root), lambda current: SystemFaceAdapter(), tmp_path / "out", stub
        )


def test_release_publishes_with_only_a_line_count_warning(tmp_path: Path) -> None:
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(
            id="u1",
            source="line one\nline two",
            target="line one line two",
            status=Status.REVIEWED,
        ),
    )

    result = release_patcher(
        project, lambda current: RepackingAdapter(), tmp_path / "artifacts", stub
    )

    (warning,) = result.qa.issues
    assert warning.check == "line-count"
    assert warning.severity == "warning"
    assert result.executable.is_file()


def test_cancelled_release_cleans_staging_and_final_artifacts(tmp_path: Path) -> None:
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(id="u1", source="hello", target="translated", status=Status.REVIEWED),
    )
    artifacts = tmp_path / "artifacts"
    flag = CancellationFlag()

    with pytest.raises(JobCancelled):
        release_patcher(
            project,
            lambda current: RepackingAdapter(),
            artifacts,
            stub,
            CancellingSink(flag),
            flag,
        )

    assert not list(artifacts.glob("*"))


def test_a_patch_carries_no_game_file_left_among_the_strings(tmp_path: Path) -> None:
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(id="u1", source="hello", target="translated", status=Status.MT),
    )
    for name in ("original.lsb", "Actors.rvdata2", "Game.rgss3a"):
        (project.strings_dir / name).write_bytes(b"game")

    built = build_patch(project, tmp_path / "dist", adapter_factory=_repacking, stub=stub)

    assert built.executable == tmp_path / "dist" / "release-ja2zh-tw-patcher.exe"
    assert set(built.files) == {
        "recipe.json",
        "proxy_map.json",
        "strings/a.lsb.json",
        "fonts/0.ttf",
    }
    assert [path.name for path in (tmp_path / "dist").iterdir()] == [built.executable.name]


def test_a_release_named_with_an_unsafe_language_tag_is_refused(tmp_path: Path) -> None:
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(id="u1", source="hello", target="translated", status=Status.MT),
    )
    project.config.target_lang = "zh/../tw"

    with pytest.raises(InvalidProject, match="Language tags"):
        build_patch(project, tmp_path / "dist", adapter_factory=_repacking, stub=stub)
    assert not (tmp_path / "dist").exists()
    assert not (project.root / "proxy_map.json").exists()




def test_run_qa_returns_status_counts_and_structured_issues(tmp_path: Path) -> None:
    project = create_project(CreateProjectRequest(tmp_path, "qa", "ja", "zh-Hant", "livemaker"))
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[
                TextUnit(
                    id="u1",
                    source="%name% line one",
                    target="one line",
                    status=Status.MT,
                ),
                TextUnit(id="u2", source="pending"),
            ],
        ),
        "a.lsb.json",
    )
    output = tmp_path / "qa.json"

    report = run_qa(project, output)

    assert report.status_counts[Status.MT] == 1
    assert report.status_counts[Status.UNTRANSLATED] == 1
    assert report.total == 2
    assert report.passed is False
    assert [(issue.unit_id, issue.check) for issue in report.issues] == [("u1", "variables")]
    assert json.loads(output.read_text(encoding="utf-8"))[0]["unit_id"] == "u1"


def test_run_qa_passes_clean_nonempty_project(tmp_path: Path) -> None:
    project = create_project(CreateProjectRequest(tmp_path, "qa", "ja", "zh-Hant", "livemaker"))
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[TextUnit(id="u1", source="pending")],
        ),
        "a.lsb.json",
    )

    assert run_qa(project).passed is True


def _patcher_release(tmp_path: Path, game: bytes, program: bytes):
    project, stub = release_project_fixture(
        tmp_path,
        TextUnit(id="u1", source="hello", target="translated", status=Status.REVIEWED),
        game,
    )
    stub.write_bytes(program)
    built = release_patcher(
        project,
        lambda current: RepackingAdapter(),
        project.root / "dist",
        stub,
    )
    return project, built


def test_a_patcher_release_leaves_only_the_patch_in_dist(tmp_path: Path) -> None:
    project, built = _patcher_release(
        tmp_path, b"MZ original game" * 512, b"MZ the patcher program"
    )

    assert [p for p in (project.root / "dist").rglob("*") if p.is_file()] == [built.executable]


@only_on_windows
def test_the_patch_executable_shows_the_games_icon(tmp_path: Path) -> None:
    game = GAME.read_bytes()
    _, built = _patcher_release(tmp_path, game, Path(sys.executable).read_bytes())

    data = built.executable.read_bytes()
    assert exe_icon(data) == exe_icon(game)
    assert read_patch(data).files[0].name == "Game.exe"


def test_a_patcher_built_before_the_engine_changed_is_not_shipped(tmp_path: Path) -> None:
    # From source the patcher is built apart: the patch would compile the
    # player's game with the engine as it was then.
    stub = tmp_path / "build" / "bokucho-patcher.exe"
    stub.parent.mkdir()
    stub.write_bytes(b"MZ")
    engine = tmp_path / "bokucho" / "engines" / "livemaker.py"
    engine.parent.mkdir(parents=True)
    engine.write_text("", encoding="utf-8")
    os.utime(stub, (1000, 1000))
    os.utime(engine, (2000, 2000))

    with pytest.raises(ReleaseBlocked, match="livemaker.py"):
        patcher_stub([stub], tmp_path / "bokucho")

    os.utime(stub, (3000, 3000))
    assert patcher_stub([stub], tmp_path / "bokucho") == stub
