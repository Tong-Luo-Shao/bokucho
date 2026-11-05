"""Gated end-to-end test against the official Ren'Py SDK (The Question)."""

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bokucho import cli

runner = CliRunner()

SDK = os.environ.get("BOKUCHO_TEST_RENPY_SDK")

pytestmark = pytest.mark.skipif(
    not SDK, reason="set BOKUCHO_TEST_RENPY_SDK to a Ren'Py SDK directory"
)


def _extract_ids(proj: Path) -> set[str]:
    ids = set()
    for f in (proj / "strings").glob("*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        ids.update(u["id"] for u in data["units"])
    return ids


def test_the_question_full_loop(tmp_path):
    sdk = Path(SDK)
    game = tmp_path / "the_question"
    shutil.copytree(sdk / "the_question", game)

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.toml").write_text(
        f'''[project]
name = "tq"
source_lang = "en"
target_lang = "zh-Hant"

[engine]
type = "renpy"
game_path = "{game.as_posix()}"
sdk_path = "{sdk.as_posix()}"
''',
        encoding="utf-8",
    )

    assert runner.invoke(cli.app, ["extract", str(proj)]).exit_code == 0
    first = _extract_ids(proj)
    assert first, "no units extracted from The Question"
    assert runner.invoke(cli.app, ["extract", str(proj)]).exit_code == 0
    assert _extract_ids(proj) == first

    marked = None
    for f in sorted((proj / "strings").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for u in data["units"]:
            if u["engine_ref"].get("kind") == "dialogue":
                u["target"] = "Bokucho測試譯文"
                u["status"] = "mt"
                marked = u["id"]
                break
        if marked:
            f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            break
    assert marked, "The Question yielded no dialogue units"

    assert runner.invoke(cli.app, ["inject", str(proj)]).exit_code == 0
    out_text = "".join(
        p.read_text(encoding="utf-8") for p in (proj / "out").rglob("*.rpy")
    )
    assert "Bokucho測試譯文" in out_text
