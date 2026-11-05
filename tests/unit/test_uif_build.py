"""The UIF winmm.dll tools/build.py ships, and where Bokucho finds it."""

from __future__ import annotations

import importlib.util
import os
import re
import struct
import sys
from pathlib import Path

import pytest

from bokucho.application.release import bundled_uif


def _build_script():
    path = Path(__file__).resolve().parents[2] / "tools" / "build.py"
    spec = importlib.util.spec_from_file_location("bokucho_build_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dll(machine: int = 0x14C, body: bytes = b"KERNEL32.dll") -> bytes:
    """A DOS stub pointing at a PE header for ``machine``, then ``body``."""
    header = 0x80
    stub = b"MZ" + bytes(0x3A) + struct.pack("<I", header) + bytes(header - 0x40)
    return stub + b"PE\0\0" + struct.pack("<H", machine) + bytes(18) + body


def test_a_32_bit_dll_carrying_its_runtime_and_writing_no_log_is_kept(tmp_path: Path) -> None:
    dll = tmp_path / "winmm.dll"
    dll.write_bytes(_dll())
    _build_script().check_uif(dll)


@pytest.mark.parametrize(
    ("data", "words"),
    [
        (b"not a library", "not a Windows DLL"),
        (_dll(machine=0x8664), "32-bit"),
        (_dll(body=b"VCRUNTIME140.dll"), "Visual C++ runtime"),
        (_dll(body=b"api-ms-win-crt-runtime-l1-1-0.dll"), "Visual C++ runtime"),
        (_dll(body=b"uif_log.log"), "uif_log.log"),
    ],
    ids=["not-a-dll", "64-bit", "vcruntime", "ucrt", "log"],
)
def test_a_dll_a_player_could_not_run_the_game_with_is_refused(
    tmp_path: Path, data: bytes, words: str
) -> None:
    dll = tmp_path / "winmm.dll"
    dll.write_bytes(data)
    with pytest.raises(RuntimeError, match=re.escape(words)):
        _build_script().check_uif(dll)


def test_the_frozen_bokucho_finds_uif_where_the_build_put_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = tmp_path / "build" / "winmm.dll"
    arguments = _build_script().pyinstaller_arguments(tmp_path / "stub.exe", built)
    assert f"--add-data={built}{os.pathsep}bokucho/uif" in arguments

    carried = tmp_path / "frozen" / "bokucho" / "uif" / "winmm.dll"
    carried.parent.mkdir(parents=True)
    carried.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)
    assert bundled_uif() == carried


def test_without_uif_built_bokucho_carries_none(tmp_path: Path) -> None:
    missing = [tmp_path / "frozen" / "winmm.dll", tmp_path / "build" / "winmm.dll"]
    assert bundled_uif(missing) is None
