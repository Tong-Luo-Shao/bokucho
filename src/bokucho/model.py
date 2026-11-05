"""Neutral data model — the backbone of the whole pipeline."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field

SCHEMA_VERSION = 2

HISTORY_CAP = 20


class Status(StrEnum):
    """Lifecycle of a single translatable line."""

    UNTRANSLATED = "untranslated"
    MT = "mt"
    REVIEWED = "reviewed"
    IGNORED = "ignored"


def _reviewed_absorbs_approved(value: Any) -> Any:
    """Read a file written while ``approved`` was still a status."""
    return Status.REVIEWED if value == "approved" else value


StoredStatus = Annotated[Status, BeforeValidator(_reviewed_absorbs_approved)]


PROTECTED_STATUSES: frozenset[Status] = frozenset({Status.REVIEWED})


class Context(BaseModel):
    """Hints fed to the LLM to raise translation quality."""

    speaker: str | None = None
    scene: str | None = None
    max_length: int | None = None
    preceding: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    """One snapshot recorded when a unit's status transitions or a run replaces its text."""

    target: str
    status: StoredStatus
    at: str


class TextUnit(BaseModel):
    """A single translatable string plus everything needed to round-trip it."""

    schema_version: int = SCHEMA_VERSION
    id: str
    source: str
    target: str = ""
    status: StoredStatus = Status.UNTRANSLATED
    kind: str = "text"
    context: Context = Field(default_factory=Context)
    engine_ref: dict[str, Any] = Field(default_factory=dict)
    history: list[HistoryEntry] = Field(default_factory=list)
    missing: bool = False

    @property
    def needs_translation(self) -> bool:
        """True if automatic translation is allowed to process this unit."""
        return self.status == Status.UNTRANSLATED

    @property
    def is_protected(self) -> bool:
        """True if this unit holds human work that must not be overwritten."""
        return self.status in PROTECTED_STATUSES


class StringFile(BaseModel):
    """A collection of :class:`TextUnit` records for one source file."""

    schema_version: int = SCHEMA_VERSION
    source_file: str
    units: list[TextUnit] = Field(default_factory=list)

    def by_id(self, unit_id: str) -> TextUnit | None:
        for unit in self.units:
            if unit.id == unit_id:
                return unit
        return None
