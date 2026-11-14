"""Unity extraction against a shipped Mono game, read only."""

import os
from pathlib import Path

import pytest

pytest.importorskip("UnityPy")

from bokucho.engines.unity import UnityAdapter  # noqa: E402

GAME = os.environ.get("BOKUCHO_TEST_UNITY", "")
SOURCE = os.environ.get("BOKUCHO_TEST_UNITY_LANG", "ja")

pytestmark = pytest.mark.skipif(
    not GAME, reason="set BOKUCHO_TEST_UNITY to a real Mono Unity game directory"
)


def test_extract_reads_text_without_changing_the_game():
    game = Path(GAME)
    before = {path: path.stat().st_mtime_ns for path in game.rglob("*") if path.is_file()}
    units = UnityAdapter(source_lang=SOURCE).extract(game)
    assert units
    assert len({unit.id for unit in units}) == len(units)
    assert {unit.engine_ref["kind"] for unit in units} & {"text_asset", "mono_behaviour"}
    after = {path: path.stat().st_mtime_ns for path in game.rglob("*") if path.is_file()}
    assert after == before
