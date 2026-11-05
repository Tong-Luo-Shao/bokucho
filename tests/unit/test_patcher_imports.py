"""The patcher is frozen on its own: whatever it imports from Bokucho would ship to players."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

PATCHER = Path(__file__).resolve().parents[2] / "src" / "bokucho" / "patcher"


def test_the_patcher_imports_only_its_runtime_compiler_and_compression() -> None:
    allowed = set(sys.stdlib_module_names) | {"__future__", "backports", "compression"}
    compiler = {
        "bokucho.application.fonts",
        "bokucho.application.projects",
        "bokucho.application.proxy_font",
        "bokucho.engines",
        "bokucho.engines.base",
        "bokucho.engines.livemaker",
        "bokucho.engines.rpgmaker",
        "bokucho.engines.rpgmaker_scripts",
        "bokucho.project",
        # The fill font is merged with fontTools, which proxy_font already brings along.
        "fontTools.ttLib",
    }
    sources = sorted(PATCHER.glob("*.py"))
    assert sources
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert (
                    name.split(".")[0] in allowed
                    or name.startswith("bokucho.patcher")
                    or name in compiler
                ), f"{path.name} imports {name}"


def test_the_patcher_is_frozen_without_the_language_detector(tmp_path: Path) -> None:
    path = PATCHER.parents[2] / "tools" / "build.py"
    spec = importlib.util.spec_from_file_location("bokucho_build_script", path)
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    assert "--exclude-module=lingua" in build.patcher_arguments()
    gui = build.pyinstaller_arguments(tmp_path / "stub.exe", tmp_path / "winmm.dll")
    assert "--exclude-module=lingua" not in gui
