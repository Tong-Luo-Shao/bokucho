"""Tests for neutral-format JSON I/O."""

from pathlib import Path

import pytest

from bokucho.model import Context, Status, StringFile, TextUnit
from bokucho.sync.json_io import (
    dump_string_file,
    load_string_file,
    load_strings_dir,
    save_string_file,
)


def _sample() -> StringFile:
    return StringFile(
        source_file="scene01.lsb",
        units=[
            TextUnit(
                id="scene01.lsb#0001",
                source="おはよう",
                target="早安",
                status=Status.MT,
                context=Context(speaker="ヒロイン"),
                engine_ref={"file": "scene01.lsb", "command_index": 1},
            ),
            TextUnit(id="scene01.lsb#0002", source="さようなら"),
        ],
    )


def test_dump_is_utf8_unescaped_and_newline_terminated():
    text = dump_string_file(_sample())
    assert "おはよう" in text
    assert text.endswith("\n")


def test_file_round_trip(tmp_path):
    original = _sample()
    path = tmp_path / "scene01.json"
    save_string_file(original, path)
    assert load_string_file(path) == original


def test_load_strings_dir_sorted(tmp_path):
    save_string_file(StringFile(source_file="b.lsb"), tmp_path / "b.json")
    save_string_file(StringFile(source_file="a.lsb"), tmp_path / "a.json")
    loaded = load_strings_dir(tmp_path)
    assert [sf.source_file for sf in loaded] == ["a.lsb", "b.lsb"]


def test_save_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "scene01.json"
    path.write_text("old", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("locked")

    monkeypatch.setattr("bokucho.sync.atomic.os.replace", fail_replace)

    with pytest.raises(OSError, match="locked"):
        save_string_file(_sample(), path)

    assert path.read_text(encoding="utf-8") == "old"
