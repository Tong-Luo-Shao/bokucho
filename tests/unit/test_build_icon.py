"""Bokucho.exe wears the ink butterfly; a patch keeps its game's icon or none of Bokucho's."""

import importlib.util
import sys
from pathlib import Path

from PIL import Image

TOOLS = Path(__file__).resolve().parents[2] / "tools"


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"bokucho_tool_{name}", TOOLS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bokucho_exe_wears_the_icon_and_the_patcher_does_not(tmp_path: Path) -> None:
    build = _tool("build")
    gui = build.pyinstaller_arguments(tmp_path / "stub.exe", tmp_path / "winmm.dll")
    assert f"--icon={build.ICON}" in gui
    assert not any(argument.startswith("--icon") for argument in build.patcher_arguments())


def test_the_committed_icon_holds_the_sizes_explorer_and_the_taskbar_ask_for() -> None:
    icon = _tool("build").ICON
    with Image.open(icon) as picture:
        assert {16, 20, 24, 32, 40, 48, 64, 256} <= {width for width, _ in picture.info["sizes"]}
