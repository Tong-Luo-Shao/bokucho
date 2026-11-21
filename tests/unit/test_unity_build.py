"""The release carries UnityPy and, once built, the TextMesh Pro fallback fonts."""

import importlib.util
import os
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"bokucho_tool_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_release_carries_the_unity_fonts_and_unitypy(tmp_path):
    build = _tool("build")
    fonts = tmp_path / "unity-fonts"
    arguments = build.pyinstaller_arguments(
        tmp_path / "stub.exe", tmp_path / "winmm.dll", tmp_path / "font", fonts
    )
    assert not any("bokucho/unity-fonts" in argument for argument in arguments)
    assert "--collect-all=UnityPy" in arguments
    assert "--hidden-import=bokucho.engines.unity" in arguments
    fonts.mkdir()
    arguments = build.pyinstaller_arguments(
        tmp_path / "stub.exe", tmp_path / "winmm.dll", tmp_path / "font", fonts
    )
    assert f"--add-data={fonts}{os.pathsep}bokucho/unity-fonts" in arguments
