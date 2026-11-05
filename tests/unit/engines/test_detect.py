"""Tests for engine detection — filename/directory sniffing, no engine deps."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from bokucho import cli
from bokucho.detect import CONFIDENT, SIGNATURES, detect
from bokucho.engines import available_engines, get_adapter
from bokucho.engines.rpgmaker import RpgMakerAdapter
from bokucho.project import Project


def _touch(root: Path, *rel: str) -> Path:
    """Create empty files (and their parents) under ``root``."""
    for r in rel:
        p = root / r
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    return root


def test_livemaker_unpacked_dir(tmp_path):
    _touch(tmp_path, "live.lpb", "00000001.lsb", "sub/00000002.lsb")
    top = detect(tmp_path)[0]
    assert top.engine == "livemaker"
    assert top.supported
    assert top.confidence == 0.95
    assert "live.lpb" in top.evidence


def test_evidence_has_no_duplicates(tmp_path):
    _touch(tmp_path, "live.lpb")
    top = detect(tmp_path)[0]
    assert top.evidence == ["live.lpb"]


def test_renpy_project_dir(tmp_path):
    _touch(tmp_path, "game/script.rpy", "game/script.rpyc")
    top = detect(tmp_path)[0]
    assert top.engine == "renpy"
    assert top.supported
    assert top.confidence == 0.95


def test_renpy_distributed_game(tmp_path):
    _touch(tmp_path, "game/archive.rpa", "renpy/__init__.py")
    top = detect(tmp_path)[0]
    assert top.engine == "renpy"
    assert top.confidence == 0.9


@pytest.mark.parametrize(("path", "expected"), [
    ("www/data/System.json", "rpgmaker-mv"),
    ("data/System.json", "rpgmaker-mz"),
    ("Data/System.rvdata2", "rpgmaker-vx-ace"),
    ("Data/System.rxdata", "rpgmaker-xp"),
    ("Game.rgssad", "rpgmaker-xp"),
    ("Data/System.rvdata", "rpgmaker-vx"),
    ("Game.rgss2a", "rpgmaker-vx"),
])
def test_rpgmaker_detection_returns_concrete_backend_type(tmp_path, path, expected):
    _touch(tmp_path, path)

    top = detect(tmp_path)[0]

    assert top.engine == expected
    assert top.supported


def test_rpgmaker_backend_types_share_adapter_and_keep_legacy_alias():
    assert set(available_engines()) >= {
        "rpgmaker-mv", "rpgmaker-mz", "rpgmaker-vx-ace", "rpgmaker-xp", "rpgmaker-vx",
    }
    assert "rpgmaker" not in available_engines()
    for engine in (
        "rpgmaker", "rpgmaker-mv", "rpgmaker-mz", "rpgmaker-vx-ace",
        "rpgmaker-xp", "rpgmaker-vx",
    ):
        assert isinstance(get_adapter(engine), RpgMakerAdapter)


def test_unsupported_engine_reports_external_tool(tmp_path):
    _touch(tmp_path, "data.xp3")
    top = detect(tmp_path)[0]
    assert top.engine == "kirikiri"
    assert not top.supported
    assert "GARbro" in top.hint


def test_every_signature_has_hint_and_patterns():
    for sig in SIGNATURES:
        assert sig.hint, f"{sig.engine} has no hint"
        assert sig.patterns, f"{sig.engine} has no patterns"


def test_empty_dir_detects_nothing(tmp_path):
    assert detect(tmp_path) == []


def test_confidence_is_max_not_sum(tmp_path):
    _touch(tmp_path, "assets.lpb")
    top = detect(tmp_path)[0]
    assert top.engine == "livemaker"
    assert top.confidence == 0.7
    assert top.confidence < CONFIDENT


def test_supported_outranks_unsupported_at_equal_confidence(tmp_path):
    _touch(tmp_path, "00000001.lsb", "data.xp3")
    results = detect(tmp_path)
    assert [d.engine for d in results[:2]] == ["livemaker", "kirikiri"]
    assert results[0].confidence == results[1].confidence == 0.95


runner = CliRunner()


def test_cli_detect_supported_shows_codec(tmp_path):
    _touch(tmp_path, "live.lpb")
    result = runner.invoke(cli.app, ["detect", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "LiveMaker" in result.output
    assert "cp932" in result.output


def test_cli_detect_renpy_codec_is_utf8(tmp_path):
    _touch(tmp_path, "game/script.rpy")
    result = runner.invoke(cli.app, ["detect", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "utf-8" in result.output


def test_cli_detect_unsupported_exits_1_without_codec(tmp_path):
    _touch(tmp_path, "data.xp3")
    result = runner.invoke(cli.app, ["detect", str(tmp_path)])
    assert result.exit_code == 1
    assert "GARbro" in result.output
    assert "utf-8" not in result.output


def test_cli_detect_nothing_suggests_unpacking(tmp_path):
    result = runner.invoke(cli.app, ["detect", str(tmp_path)])
    assert result.exit_code == 1
    assert "unpack" in result.output.lower()


def test_cli_detect_low_confidence_still_exits_0(tmp_path):
    _touch(tmp_path, "assets.lpb")
    result = runner.invoke(cli.app, ["detect", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "confidence" in result.output.lower()


def test_cli_detect_rejects_non_directory(tmp_path):
    f = tmp_path / "Game.exe"
    f.write_text("", encoding="utf-8")
    result = runner.invoke(cli.app, ["detect", str(f)])
    assert result.exit_code == 1


def test_init_game_fills_engine_and_path(tmp_path):
    game = _touch(tmp_path / "unpacked", "live.lpb", "00000001.lsb")
    proj = tmp_path / "proj"
    result = runner.invoke(cli.app, ["init", str(proj), "--game", str(game)])
    assert result.exit_code == 0, result.output
    project = Project.load(proj)
    assert project.config.engine.type == "livemaker"
    assert Path(project.config.engine.game_path) == game.resolve()


def test_init_game_unidentified_aborts_without_creating_project(tmp_path):
    game = tmp_path / "mystery"
    game.mkdir()
    proj = tmp_path / "proj"
    result = runner.invoke(cli.app, ["init", str(proj), "--game", str(game)])
    assert result.exit_code == 1
    assert not (proj / "project.toml").exists()


def test_init_explicit_engine_beats_detection(tmp_path):
    game = _touch(tmp_path / "unpacked", "live.lpb")
    proj = tmp_path / "proj"
    result = runner.invoke(
        cli.app, ["init", str(proj), "--game", str(game), "--engine", "renpy"]
    )
    assert result.exit_code == 0, result.output
    assert Project.load(proj).config.engine.type == "renpy"


def test_init_explicit_engine_survives_failed_detection(tmp_path):
    game = tmp_path / "mystery"
    game.mkdir()
    proj = tmp_path / "proj"
    result = runner.invoke(
        cli.app, ["init", str(proj), "--game", str(game), "--engine", "renpy"]
    )
    assert result.exit_code == 0, result.output
    assert Project.load(proj).config.engine.type == "renpy"


def test_init_game_rejects_non_directory(tmp_path):
    f = tmp_path / "Game.exe"
    f.write_text("", encoding="utf-8")
    result = runner.invoke(cli.app, ["init", str(tmp_path / "proj"), "--game", str(f)])
    assert result.exit_code == 1


def _packed_exe(path: Path, *, trailer: bytes = b"lv") -> Path:
    """A minimal MS executable carrying a LiveMaker archive trailer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\x00" * 64 + b"\x00\x00\x00\x00" + trailer)
    return path


def test_packed_livemaker_executable_is_detected(tmp_path):
    exe = _packed_exe(tmp_path / "MyGame.exe")
    top = detect(exe)[0]
    assert top.engine == "livemaker"
    assert top.supported
    assert top.confidence >= CONFIDENT
    assert top.evidence == ["MyGame.exe"]


def test_packed_archive_in_a_directory_is_detected(tmp_path):
    _packed_exe(tmp_path / "MyGame.exe")
    top = detect(tmp_path)[0]
    assert top.engine == "livemaker"
    assert top.evidence == ["MyGame.exe"]


def test_plain_executable_is_not_livemaker(tmp_path):
    _packed_exe(tmp_path / "Other.exe", trailer=b"xx")
    assert detect(tmp_path / "Other.exe") == []


def test_detecting_a_file_also_scans_its_siblings(tmp_path):
    _touch(tmp_path, "game/script.rpy")
    launcher = tmp_path / "MyGame.exe"
    launcher.write_bytes(b"MZ" + b"\x00" * 16)
    assert detect(launcher)[0].engine == "renpy"
