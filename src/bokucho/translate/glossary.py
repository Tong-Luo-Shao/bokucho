"""Glossary — proper nouns, character names, tone notes."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from bokucho.model import SCHEMA_VERSION
from bokucho.sync.atomic import atomic_write_text


class GlossaryEntry(BaseModel):
    source: str
    target: str
    note: str | None = None


class Glossary(BaseModel):
    schema_version: int = SCHEMA_VERSION
    entries: list[GlossaryEntry] = Field(default_factory=list)

    def as_mapping(self) -> dict[str, str]:
        """Flatten to a ``source -> target`` dict for feeding a translator."""
        return {e.source: e.target for e in self.entries}

    @classmethod
    def load(cls, path: str | Path) -> Glossary:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def load_or_empty(cls, path: str | Path) -> Glossary:
        """Load from ``path``, or an empty glossary when the file is absent."""
        path = Path(path)
        return cls.load(path) if path.is_file() else cls()

    def save(self, path: str | Path) -> None:
        atomic_write_text(path, self.model_dump_json(indent=2) + "\n")


GLOBAL_DIR_ENV = "BOKUCHO_HOME"


def global_dir() -> Path:
    """User-level bokucho directory shared by all projects ($BOKUCHO_HOME overrides)."""
    env = os.environ.get(GLOBAL_DIR_ENV)
    return Path(env) if env else Path.home() / ".bokucho"


def global_glossary_path() -> Path:
    return global_dir() / "glossary.json"


def global_terms_path() -> Path:
    return global_dir() / "terms.tsv"


def load_global_glossary() -> Glossary:
    """The user-level glossary; empty when the file does not exist."""
    return Glossary.load_or_empty(global_glossary_path())


def merged_mapping(project: Glossary, global_: Glossary) -> dict[str, str]:
    """Effective ``source -> target``: project entries override global ones."""
    return {**global_.as_mapping(), **project.as_mapping()}
