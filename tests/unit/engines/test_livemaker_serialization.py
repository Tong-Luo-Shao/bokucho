"""Command dispatch keeps the original binary and validation contracts."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

pytest.importorskip("livemaker")

from livemaker.exceptions import BadLsbError  # noqa: E402
from livemaker.lsb.command import _command_structs  # noqa: E402

from bokucho.engines.livemaker import LiveMakerAdapter  # noqa: E402

SCENE = (
    struct.pack("<IBIII", 117, 0, 64, 0, 1)
    + struct.pack("<BIBBII", 3, 0, 0, 0, 1, 5)
    + b"scene"
)


def test_a_command_is_read_and_written_without_trying_other_command_types(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "scene.lsb"
    path.write_bytes(SCENE)
    unused = _command_structs[0]
    parse, build = unused._parse, unused._build
    tried = []

    def tracked_parse(*args):
        tried.append("parse")
        return parse(*args)

    def tracked_build(*args):
        tried.append("build")
        return build(*args)

    monkeypatch.setattr(unused, "_parse", tracked_parse)
    monkeypatch.setattr(unused, "_build", tracked_build)
    script = LiveMakerAdapter._open(path)
    assert script.commands[0]["Name"] == "scene"
    assert LiveMakerAdapter._serialize_validated(script) == SCENE
    assert tried == []


@pytest.mark.parametrize("data", [SCENE[:-1], SCENE[:17] + b"\xff" + SCENE[18:]])
def test_incomplete_and_unknown_commands_are_rejected(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "scene.lsb"
    path.write_bytes(data)
    with pytest.raises(BadLsbError):
        LiveMakerAdapter._open(path)


def test_invalid_scenario_metadata_uses_the_standard_script_error(tmp_path: Path) -> None:
    body = b"TpWordxxx"
    path = tmp_path / "scene.lsb"
    path.write_bytes(
        SCENE[:17] + struct.pack("<BIBBI", 20, 0, 0, 0, 1)
        + struct.pack("<I", len(body)) + body
    )
    with pytest.raises(BadLsbError):
        LiveMakerAdapter._open(path)


@pytest.mark.parametrize("version", [105, 117])
def test_version_specific_command_fields_keep_their_parent_context(
    tmp_path: Path, version: int
) -> None:
    body = b"TpWord105" + struct.pack("<IIII", 0, 0, 0, 0)
    source = (
        struct.pack("<IBIII", version, 0, 64, 0, 1)
        + struct.pack("<BIBBI", 20, 0, 0, 0, 1)
        + struct.pack("<I", len(body)) + body
        + bytes(12 + (4 if version > 106 else 0))
    )
    path = tmp_path / "scene.lsb"
    path.write_bytes(source)
    script = LiveMakerAdapter._open(path)
    assert (script.commands[0]["StopEvent"] is None) == (version <= 106)
    assert LiveMakerAdapter._serialize_validated(script) == source
