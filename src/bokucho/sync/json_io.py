"""JSON I/O for the neutral format."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bokucho.model import StringFile
from bokucho.sync.atomic import atomic_write_text, file_lock

STRINGS_GLOB = "*.json"


def dump_string_file(string_file: StringFile) -> str:
    """Serialize a :class:`StringFile` to pretty, human-diffable JSON."""
    return string_file.model_dump_json(indent=2) + "\n"


def save_string_file(string_file: StringFile, path: str | Path) -> None:
    """Write a :class:`StringFile` to ``path`` as UTF-8 JSON."""
    atomic_write_text(path, dump_string_file(string_file))


def load_string_file(path: str | Path) -> StringFile:
    """Read and validate a :class:`StringFile` from ``path``."""
    text = Path(path).read_text(encoding="utf-8")
    return StringFile.model_validate_json(text)


def string_paths(directory: str | Path) -> list[Path]:
    """Every ``*.json`` string file in ``directory``, sorted by name."""
    return sorted(Path(directory).glob(STRINGS_GLOB))


def load_strings_dir(directory: str | Path) -> list[StringFile]:
    """Load every ``*.json`` string file in ``directory`` (sorted by name)."""
    return [load_string_file(p) for p in string_paths(directory)]


@contextmanager
def held_string_file(path: str | Path) -> Iterator[StringFile]:
    """The file as it is now, kept from every other write until the block ends."""
    with file_lock(path):
        yield load_string_file(path)
