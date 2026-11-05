from __future__ import annotations

import threading
from pathlib import Path

import pytest

import bokucho.sync.atomic as atomic
from bokucho.sync.atomic import atomic_write_text


def test_atomic_writes_to_the_same_destination_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "strings.json"
    path.write_text("old", encoding="utf-8")
    original_replace = atomic.os.replace
    first_replace_started = threading.Event()
    release_first_replace = threading.Event()
    second_replace_started = threading.Event()
    replace_count = 0
    errors: list[BaseException] = []

    def controlled_replace(source: Path, destination: Path) -> None:
        nonlocal replace_count
        replace_count += 1
        if replace_count == 1:
            first_replace_started.set()
            assert release_first_replace.wait(timeout=2)
        else:
            second_replace_started.set()
        original_replace(source, destination)

    def write(text: str) -> None:
        try:
            atomic_write_text(path, text)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(atomic.os, "replace", controlled_replace)
    first = threading.Thread(target=write, args=("first",))
    second = threading.Thread(target=write, args=("second",))

    first.start()
    assert first_replace_started.wait(timeout=2)
    second.start()

    assert not second_replace_started.wait(timeout=0.1)
    release_first_replace.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_replace_started.is_set()
    assert path.read_text(encoding="utf-8") == "second"
