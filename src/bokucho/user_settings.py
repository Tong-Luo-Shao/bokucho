"""Atomic user-level GUI preferences stored outside projects."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from bokucho.appdirs import app_data_dir
from bokucho.project import TranslatorInterfaceConfig
from bokucho.sync.atomic import atomic_write_text


class RecentProjectRecord(BaseModel):
    path: str
    last_opened: datetime


class GuiSettings(BaseModel):
    locale: Literal["zh_TW", "en"] = "zh_TW"
    theme: Literal["dark", "light"] = "dark"
    accent: Literal["clay", "lime", "amber", "ice"] = "clay"
    density: Literal["compact", "normal", "roomy"] = "normal"
    nav_expanded: bool = False
    font_family: str = Field(default="", max_length=128, pattern=r"^[^\x00-\x1f\x7f]*$")
    font_size: int = Field(default=16, ge=12, le=24)
    recent_projects: list[RecentProjectRecord] = Field(default_factory=list)
    window_geometry: str | None = None
    reduced_motion: bool = False
    ocr_batch_size: int | None = Field(default=None, ge=1, le=64)
    ocr_detection_threshold: float = Field(
        default=0.9, ge=0, le=1, allow_inf_nan=False
    )
    ocr_recognition_threshold: float = Field(
        default=0.9, ge=0, le=1, allow_inf_nan=False
    )
    interfaces: list[TranslatorInterfaceConfig] = Field(default_factory=list)
    deleted_interfaces: list[str] = Field(default_factory=list)


def _normalized_path(path: str | Path) -> str:
    return Path(path).expanduser().resolve(strict=False).as_posix()


def _without(
    records: list[RecentProjectRecord], normalized: str
) -> list[RecentProjectRecord]:
    return [item for item in records if item.path.casefold() != normalized.casefold()]


class UserSettingsStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else self.default_path()

    @staticmethod
    def default_path() -> Path:
        return app_data_dir() / "settings.json"

    def load(self) -> GuiSettings:
        if not self.path.is_file():
            return GuiSettings()
        try:
            text = self.path.read_text(encoding="utf-8")
            return GuiSettings.model_validate_json(text)
        except (OSError, UnicodeError, ValidationError):
            self._quarantine_invalid_file()
            return GuiSettings()

    def save(self, settings: GuiSettings) -> None:
        atomic_write_text(self.path, settings.model_dump_json(indent=2) + "\n")

    def record_project(self, path: str | Path) -> RecentProjectRecord:
        normalized = _normalized_path(path)
        settings = self.load()
        settings.recent_projects = _without(settings.recent_projects, normalized)
        record = RecentProjectRecord(
            path=normalized,
            last_opened=datetime.now(UTC),
        )
        settings.recent_projects.insert(0, record)
        self.save(settings)
        return record

    def remove_project(self, path: str | Path) -> bool:
        normalized = _normalized_path(path)
        settings = self.load()
        retained = _without(settings.recent_projects, normalized)
        if len(retained) == len(settings.recent_projects):
            return False
        settings.recent_projects = retained
        self.save(settings)
        return True

    def remember_interface(self, interface: TranslatorInterfaceConfig) -> None:
        settings = self.load()
        for index, item in enumerate(settings.interfaces):
            if item.id == interface.id:
                settings.interfaces[index] = interface
                break
        else:
            settings.interfaces.append(interface)
        self.save(settings)

    def forget_interface(self, interface_id: str) -> None:
        settings = self.load()
        settings.interfaces = [item for item in settings.interfaces if item.id != interface_id]
        if interface_id not in settings.deleted_interfaces:
            settings.deleted_interfaces.append(interface_id)
        self.save(settings)

    def update_ocr_configuration(
        self,
        batch_size: int | None,
        detection_threshold: float,
        recognition_threshold: float,
    ) -> None:
        settings = self.load()
        settings.ocr_batch_size = batch_size
        settings.ocr_detection_threshold = detection_threshold
        settings.ocr_recognition_threshold = recognition_threshold
        self.save(settings)

    def _quarantine_invalid_file(self) -> None:
        candidate = self.path.with_name(f"{self.path.name}.invalid")
        sequence = 2
        while candidate.exists():
            candidate = self.path.with_name(f"{self.path.name}.invalid-{sequence}")
            sequence += 1
        with contextlib.suppress(OSError):
            self.path.replace(candidate)
