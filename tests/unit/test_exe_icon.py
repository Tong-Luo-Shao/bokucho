"""A patch executable shows the game's own icon."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from bokucho.exe_icon import exe_icon, with_icon

only_on_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows' own resource writer draws the icon"
)
GAME = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"


def test_a_file_that_is_no_program_has_no_icon() -> None:
    assert exe_icon(b"MZ the patcher program") is None
    assert exe_icon(b"") is None


@only_on_windows
def test_the_program_takes_the_games_icon_and_keeps_what_follows_it() -> None:
    game = exe_icon(GAME.read_bytes())
    assert game is not None
    program = Path(sys.executable).read_bytes() + b"archive and patch"

    drawn = with_icon(program, game)

    assert exe_icon(drawn) == game != exe_icon(program)
    assert drawn.endswith(b"archive and patch")
