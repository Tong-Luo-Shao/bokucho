from __future__ import annotations

import pytest

from bokucho.patcher.payload import (
    FileState,
    NotAPatch,
    Patch,
    PatchAdded,
    PatchedFile,
    patch_executable,
    read_patch,
)

PROGRAM = b"MZ the patcher program"


def _patch(name: str = "Game.exe") -> Patch:
    return Patch(
        "テスト 中文化補丁",
        (
            PatchedFile(name, FileState(10, "a" * 64), FileState(12, "b" * 64), b"delta-1", 20),
            PatchedFile("live.dll", FileState(3, "c" * 64), FileState(4, "d" * 64), b"d2", 10),
        ),
    )


def test_the_patch_reads_back_from_the_end_of_the_executable() -> None:
    executable = patch_executable(PROGRAM, _patch())
    assert executable.startswith(PROGRAM)
    assert read_patch(executable) == _patch()


def test_a_download_cut_short_is_not_a_patch() -> None:
    executable = patch_executable(PROGRAM, _patch())
    for cut in (executable[:-1], executable[: len(executable) // 2], b"", PROGRAM):
        with pytest.raises(NotAPatch):
            read_patch(cut)


def test_a_file_name_that_leaves_the_game_folder_is_refused() -> None:
    for name in ("../Game.exe", "sub/Game.exe", "..", ""):
        with pytest.raises(NotAPatch):
            read_patch(patch_executable(PROGRAM, _patch(name)))


def _patch_adding(name: str = "winmm.dll") -> Patch:
    base = _patch()
    return Patch(
        base.title,
        base.files,
        (PatchAdded(name, b"MZ uif"), PatchAdded("uif_config.json", b"{}")),
    )


def test_added_files_ride_after_the_differences_and_read_back() -> None:
    patch = _patch_adding()
    assert read_patch(patch_executable(PROGRAM, patch)) == patch


def test_a_patch_adding_nothing_reads_back_with_nothing() -> None:
    assert read_patch(patch_executable(PROGRAM, _patch())).added == ()


def test_an_added_name_that_leaves_the_game_folder_is_refused() -> None:
    for name in ("../winmm.dll", "sub/winmm.dll", "..\\winmm.dll", "..", ""):
        with pytest.raises(NotAPatch):
            read_patch(patch_executable(PROGRAM, _patch_adding(name)))


def test_a_damaged_added_file_is_not_a_patch() -> None:
    executable = bytearray(patch_executable(PROGRAM, _patch_adding()))
    executable[executable.index(b"MZ uif")] ^= 0xFF
    with pytest.raises(NotAPatch):
        read_patch(bytes(executable))
