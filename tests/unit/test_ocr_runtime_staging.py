"""The OCR bootstrap is built for the embedded Python a release ships."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

TOOL = Path(__file__).resolve().parents[2] / "tools" / "fetch_ocr_runtime.py"


def _tool():
    spec = importlib.util.spec_from_file_location("fetch_ocr_runtime_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_bootstrap_is_installed_for_the_embedded_python(monkeypatch) -> None:
    tool = _tool()
    commands: list[list[str]] = []
    monkeypatch.setattr(tool.subprocess, "run", lambda command, **kwargs: commands.append(command))

    tool._uv_install(Path("site-packages"))

    assert len(commands) == 2
    for command in commands:
        assert command[command.index("--python-version") + 1] == tool.PYTHON_VERSION
        assert command[command.index("--python-platform") + 1] == "x86_64-pc-windows-msvc"


def test_the_bootstrap_check_loads_the_compiled_packages(monkeypatch, tmp_path: Path) -> None:
    tool = _tool()
    commands: list[list[str]] = []
    versions = {
        "accelerate": tool.OCR_ACCELERATE_VERSION,
        "pip": tool.OCR_PIP_VERSION,
        "transformers": tool.SHARED_TRANSFORMERS_VERSION,
    }

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout=json.dumps(versions))

    monkeypatch.setattr(tool.subprocess, "run", run)

    tool._verify_bootstrap(tmp_path)

    [command] = commands
    script = ast.parse(command[command.index("-c") + 1])
    imported = {
        alias.name.partition(".")[0]
        for node in ast.walk(script)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert {"numpy", "PIL", "psutil", "safetensors", "yaml", "fontTools"} <= imported
