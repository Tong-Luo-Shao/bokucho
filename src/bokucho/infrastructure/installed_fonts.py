"""Fonts installed on this machine that can typeset a project's pictures."""

from __future__ import annotations

import logging
import os
import struct
from array import array
from bisect import bisect_left
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

from bokucho.appdirs import BUNDLED_FONT_PREFIX, bundled_font_dir

FONT_SUFFIXES = frozenset({".ttf", ".otf", ".ttc"})


@dataclass(frozen=True)
class InstalledFont:
    path: str
    name: str


def font_dirs() -> list[Path]:
    """Where this machine keeps its fonts, whichever platform it is."""
    home = Path.home()
    windows = os.environ.get("WINDIR")
    local = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(windows) / "Fonts" if windows else None,
        Path(local) / "Microsoft" / "Windows" / "Fonts" if local else None,
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        home / ".local" / "share" / "fonts",
        home / ".fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        home / "Library" / "Fonts",
    ]
    return [path for path in candidates if path is not None and path.is_dir()]


def _open(path: Path) -> TTFont:
    return TTFont(path, fontNumber=0, lazy=True)


def _name(font: TTFont) -> str | None:
    names = font["name"]
    return names.getBestFullName() or names.getBestFamilyName()


def font_name(path: str | Path) -> str | None:
    """The name a face goes by, or None if ``path`` is not a font that opens."""
    try:
        with _quiet(), _open(Path(path)) as font:
            return _name(font)
    except Exception:  # noqa: BLE001 - whatever the file is, it is not a usable font
        return None


def fonts_covering(text: str, dirs: Sequence[Path] | None = None) -> list[InstalledFont]:
    """The installed faces that draw every character of ``text``, by name."""
    characters = "".join(sorted({c for c in text if not c.isspace()}))
    return list(_covering(characters, tuple(font_dirs() if dirs is None else dirs)))


def bundled_fonts_covering(text: str, folder: Path | None = None) -> list[InstalledFont]:
    """The faces Bokucho ships that draw every character of ``text``."""
    folder = bundled_font_dir() if folder is None else folder
    return [
        InstalledFont(BUNDLED_FONT_PREFIX + Path(font.path).name, font.name)
        for font in fonts_covering(text, [folder])
        if Path(font.path).parent == folder
    ]


def font_families(dirs: Sequence[Path] | None = None) -> list[str]:
    """Every family name an installed face goes by, for the interface font."""
    return list(_families(tuple(font_dirs() if dirs is None else dirs)))


def _covering(characters: str, dirs: tuple[Path, ...]) -> tuple[InstalledFont, ...]:
    needed = {ord(c) for c in characters}
    return tuple(
        font
        for font, coverage in _catalog(dirs)
        if all(
            (index := bisect_left(coverage, code)) < len(coverage) and coverage[index] == code
            for code in needed
        )
    )


# ponytail: one catalog per font folder set for the session; fonts installed
# while the program runs are offered after a restart.
@lru_cache(maxsize=8)
def _catalog(dirs: tuple[Path, ...]) -> tuple[tuple[InstalledFont, array], ...]:
    found = []
    with _quiet():
        for path in _font_files(dirs):
            try:
                with _open(path) as font:
                    font.setGlyphOrder(
                        [".notdef", *(f"glyph{i}" for i in range(1, font["maxp"].numGlyphs))]
                    )
                    coverage = array("I", sorted(font.getBestCmap() or {}))
                    if name := _name(font):
                        found.append((InstalledFont(path.as_posix(), name), coverage))
            except Exception:  # noqa: BLE001 - a broken file among the fonts is skipped
                continue
    return tuple(sorted(found, key=lambda entry: (entry[0].name.casefold(), entry[0].path)))


@lru_cache(maxsize=8)
def _families(dirs: tuple[Path, ...]) -> tuple[str, ...]:
    found: set[str] = set()
    with _quiet():
        for path in _font_files(dirs):
            try:
                if path.suffix.lower() == ".ttc":
                    with TTCollection(path, lazy=True) as collection:
                        for font in collection.fonts:
                            found |= _family_names(font)
                else:
                    with _open(path) as font:
                        found |= _family_names(font)
            except Exception:  # noqa: BLE001 - a broken file among the fonts is skipped
                continue
    return tuple(sorted(found, key=str.casefold))


def _family_names(font: TTFont) -> set[str]:
    cmap = font.reader["cmap"]
    records = struct.unpack_from(">H", cmap, 2)[0]
    if any(struct.unpack_from(">HH", cmap, 4 + 8 * i) == (3, 0) for i in range(records)):
        return set()
    names = font["name"]
    return {name for name in (names.getDebugName(1), names.getBestFamilyName()) if name}


def _font_files(dirs: tuple[Path, ...]) -> list[Path]:
    return sorted(
        path
        for folder in dirs
        for path in folder.rglob("*")
        if path.suffix.lower() in FONT_SUFFIXES and path.is_file()
    )


@contextmanager
def _quiet() -> Iterator[None]:
    """Hold back fontTools' complaints about the odd table in a system font."""
    logger = logging.getLogger("fontTools")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(level)
