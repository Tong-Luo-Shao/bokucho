from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bokucho.crash_logging import CrashLogger, SecretRedactor
from bokucho.user_settings import GuiSettings, UserSettingsStore


def test_recent_projects_are_unique_and_most_recent_first(tmp_path: Path) -> None:
    store = UserSettingsStore(tmp_path / "settings.json")
    first = (tmp_path / "games" / "a").resolve()
    second = (tmp_path / "games" / "b").resolve()

    store.record_project(first)
    store.record_project(second)
    store.record_project(first)

    assert [item.path for item in store.load().recent_projects] == [
        first.as_posix(),
        second.as_posix(),
    ]


def test_settings_round_trip_gui_preferences(tmp_path: Path) -> None:
    store = UserSettingsStore(tmp_path / "settings.json")
    settings = GuiSettings(
        locale="en",
        window_geometry="encoded-window-state",
        reduced_motion=True,
        nav_expanded=True,
    )

    store.save(settings)

    assert store.load() == settings


def test_ocr_thresholds_default_and_save_independently(tmp_path: Path) -> None:
    store = UserSettingsStore(tmp_path / "settings.json")

    assert store.load().ocr_detection_threshold == 0.9
    assert store.load().ocr_recognition_threshold == 0.9

    store.update_ocr_configuration(
        batch_size=6,
        detection_threshold=0.83,
        recognition_threshold=0.94,
    )

    settings = store.load()
    assert settings.ocr_batch_size == 6
    assert settings.ocr_detection_threshold == 0.83
    assert settings.ocr_recognition_threshold == 0.94


def test_removed_ocr_fields_do_not_quarantine_existing_settings(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        """{
  "locale": "en",
  "reduced_motion": true,
  "ocr": {"size": "4B", "variant": "Instruct", "precision": "INT4"},
  "ocr_detector": {"size": "1B", "variant": "PaddleOCR-VL", "precision": "INT8"},
  "ocr_prompt": "old prompt",
  "ocr_detector_prompt": "old detector prompt"
}
""",
        encoding="utf-8",
    )
    store = UserSettingsStore(path)

    settings = store.load()

    assert settings.locale == "en"
    assert settings.reduced_motion is True
    assert path.exists()
    assert list(tmp_path.glob("settings.json.invalid*")) == []


def test_unreadable_settings_recover_even_if_quarantine_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(GuiSettings(locale="en").model_dump_json(), encoding="utf-8")
    store = UserSettingsStore(path)
    original_read_text = Path.read_text

    def fail_settings_read(candidate: Path, *args, **kwargs) -> str:
        if candidate == path:
            raise OSError("settings locked")
        return original_read_text(candidate, *args, **kwargs)

    def fail_quarantine(*_args, **_kwargs) -> None:
        raise OSError("settings locked")

    monkeypatch.setattr(Path, "read_text", fail_settings_read)
    monkeypatch.setattr(Path, "replace", fail_quarantine)

    assert store.load() == GuiSettings()
    assert path.exists()



def test_secret_redactor_strips_credentials_and_provider_payloads() -> None:
    redactor = SecretRedactor(["sentinel-secret"])
    text = """Authorization: Bearer sentinel-secret
Provider request body: {"messages": ["private source"]}
ordinary sentinel-secret text
"""

    sanitized = redactor.redact(text)

    assert "sentinel-secret" not in sanitized
    assert "private source" not in sanitized
    assert sanitized.count("[REDACTED]") >= 3


def test_crash_logger_keeps_secret_out_of_diagnostics_and_disk(tmp_path: Path) -> None:
    logger = CrashLogger(tmp_path / "logs", SecretRedactor(["sentinel-secret"]))

    try:
        raise RuntimeError("Authorization: Bearer sentinel-secret")
    except RuntimeError as exc:
        report = logger.capture(exc)

    assert "sentinel-secret" not in report.diagnostic
    assert "sentinel-secret" not in report.path.read_text(encoding="utf-8")



def test_app_data_dir_honors_localappdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    from bokucho.appdirs import app_data_dir

    result = app_data_dir()

    assert result.is_relative_to(tmp_path)
