"""The face a save remembers is set over when the game starts."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("livemaker")

from livemaker.lpb import LMProject  # noqa: E402
from livemaker.lsb import LMScript  # noqa: E402
from livemaker.lsb.command import Jump, LabelReference, Wait  # noqa: E402

from bokucho.application.errors import JobCancelled  # noqa: E402
from bokucho.engines.livemaker import LiveMakerAdapter  # noqa: E402

START = "ノベルシステム\START.lsb"
SETTINGS = LMProject(init_lsb=START).to_lpb()


def _jump(line: int, page: str, label: int) -> Jump:
    command = Jump(LineNo=line)
    command.args["Page"] = LabelReference(page, label)
    return command


def _game(tmp_path: Path) -> tuple[Path, dict[str, LMScript], LiveMakerAdapter, list[str]]:
    """An overlay, its scripts, an adapter over them, and the scripts it writes."""
    overlay = tmp_path / "out"
    (overlay / "ノベルシステム").mkdir(parents=True)
    scripts = {
        "START.lsb": LMScript(
            call_name="START.lsb",
            commands=[Wait(LineNo=0), Wait(LineNo=2), _jump(5, START, 2)],
        ),
        "00000001.lsb": LMScript(
            call_name="00000001.lsb",
            commands=[_jump(1, START, 0), _jump(2, START, 5), _jump(3, "ゲームメイン.lsb", 2)],
        ),
    }
    (overlay / "ノベルシステム" / "START.lsb").write_bytes(b"stub")
    (overlay / "00000001.lsb").write_bytes(b"stub")
    adapter = LiveMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._open = lambda path: scripts[path.name]
    written: list[str] = []
    adapter._serialize_validated = lambda lsb: written.append(lsb.call_name) or b""
    return overlay, scripts, adapter, written


def _started(tmp_path: Path, *faces: str) -> dict[str, LMScript]:
    """The overlay's scripts after the start script is pointed at each face in turn."""
    overlay, scripts, adapter, _ = _game(tmp_path)
    for face in faces:
        adapter.start_with_face(overlay, SETTINGS, face)
    return scripts


class _CancelledAt:
    """A token cancelled at its ``count``-th check."""

    def __init__(self, count: int) -> None:
        self.left = count

    def raise_if_cancelled(self) -> None:
        self.left -= 1
        if self.left == 0:
            raise JobCancelled()


def test_the_start_script_sets_the_face_before_anything_else(tmp_path: Path) -> None:
    scripts = _started(tmp_path, "Bokucho-a1b2c3")
    start = scripts["START.lsb"].commands
    assert str(start[0]) == 'Calc StatusFontName = "Bokucho-a1b2c3"'
    assert [command.LineNo for command in start] == [0, 1, 3, 6]
    assert start[3]["Page"].Label == 3
    assert [command["Page"].Label for command in scripts["00000001.lsb"].commands] == [0, 6, 2]


def test_a_second_run_renames_the_face_and_moves_nothing(tmp_path: Path) -> None:
    scripts = _started(tmp_path, "Bokucho-a1b2c3", "Bokucho-d4e5f6")
    start = scripts["START.lsb"].commands
    assert str(start[0]) == 'Calc StatusFontName = "Bokucho-d4e5f6"'
    assert [command.LineNo for command in start] == [0, 1, 3, 6]
    assert [command["Page"].Label for command in scripts["00000001.lsb"].commands] == [0, 6, 2]


def test_a_run_cancelled_halfway_writes_nothing(tmp_path: Path) -> None:
    overlay, _, adapter, written = _game(tmp_path)
    with pytest.raises(JobCancelled):
        adapter.start_with_face(overlay, SETTINGS, "Bokucho-a1b2c3", token=_CancelledAt(2))
    assert written == []
