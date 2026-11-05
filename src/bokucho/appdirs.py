"""Qt-free default application-data directory resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BUNDLED_FONT_PREFIX = "bundled:"


def app_data_dir(app: str = "Bokucho") -> Path:
    """Per-user application-data directory for ``app``."""
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(root) / app
    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / app


def portable_root() -> Path:
    """Return the writable root carried by the portable application."""
    override = os.environ.get("BOKUCHO_PORTABLE_ROOT")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    if "__compiled__" in globals() or getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundled_font_dir() -> Path:
    """The fonts Bokucho ships: beside the program, or a checkout's ``fonts/``."""
    return portable_root() / "fonts"


def locate_font(ref: str | None) -> str | None:
    """``ref`` as a path to open; a shipped font's name becomes its file."""
    if ref is None or not ref.startswith(BUNDLED_FONT_PREFIX):
        return ref
    name = ref.removeprefix(BUNDLED_FONT_PREFIX)
    if not name or Path(name).name != name:
        return ref
    return (bundled_font_dir() / name).as_posix()
