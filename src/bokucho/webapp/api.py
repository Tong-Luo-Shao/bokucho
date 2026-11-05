"""Api -- the sole method-call surface the frontend invokes (exposed to pywebview by name)."""

from __future__ import annotations

import base64
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from bokucho import images as images_mod
from bokucho import render as render_mod
from bokucho.appdirs import BUNDLED_FONT_PREFIX, locate_font
from bokucho.application.assets import load_image_units, load_inventory, set_images_ignored
from bokucho.application.contracts import CredentialStore
from bokucho.application.errors import AppError, JobBusy, ValidationFailure
from bokucho.application.glossary import (
    delete_glossary,
    glossary_occurrences,
    glossary_spans,
    list_glossary_entries,
    upsert_glossary,
)
from bokucho.application.image_editing import load_image_document
from bokucho.application.projects import (
    CreateProjectRequest,
    load_project_summary,
    resolve_game_path,
    string_filename,
)
from bokucho.application.projects import create_project as create_project_use_case
from bokucho.application.proxy_font import (
    UIF_DLL,
    required_characters,
    sample_lines,
    uif_problem,
)
from bokucho.application.quality import bracket_fix_report, qa_report
from bokucho.application.release import bundled_uif
from bokucho.application.suggestions import TranslationMemory, suggestions_for
from bokucho.application.text import import_translations, recheck_report, save_target
from bokucho.application.text import mark_reviewed as mark_unit_reviewed
from bokucho.application.text import set_status as set_status_use_case
from bokucho.detect import CONFIDENT, detect
from bokucho.engines import available_engines
from bokucho.infrastructure import installed_fonts
from bokucho.infrastructure.credentials import (
    credential_account,
    default_credential_store,
    interface_credential_scope,
    shared_provider_credentials,
)
from bokucho.infrastructure.factories import make_engine_adapter, make_image_codec
from bokucho.infrastructure.model_catalog import (
    fetch_provider_models as fetch_models_from_provider,
)
from bokucho.infrastructure.pp_ocr import (
    DEFAULT_BATCH_SIZE,
    DETECTOR_CHECKPOINT,
    RECOGNIZER_CHECKPOINT,
)
from bokucho.language import matches_language_tag, matches_source_language
from bokucho.model import Status, TextUnit
from bokucho.project import (
    Project,
    ProjectConfig,
    TranslatorConfig,
    TranslatorInterfaceConfig,
)
from bokucho.qa import Issue, translation_tokens
from bokucho.sync.atomic import OverwriteRefused, file_lock, written_here
from bokucho.sync.json_io import load_string_file
from bokucho.translate.prompts import PromptPreset
from bokucho.translate.prompts import delete_preset as delete_prompt_preset
from bokucho.translate.prompts import find_preset as find_prompt_preset
from bokucho.translate.prompts import load_library as load_prompt_library
from bokucho.translate.prompts import save_preset as save_prompt_preset
from bokucho.translators import (
    PROVIDER_PRESETS,
    get_provider_preset,
    provider_default_base_url,
)
from bokucho.user_settings import GuiSettings
from bokucho.webapp import i18n
from bokucho.webapp import registry as registry_module
from bokucho.webapp.events import EventBus
from bokucho.webapp.image_editor import load_editor_state, save_editor_state
from bokucho.webapp.jobs import JobRunner, JobState
from bokucho.webapp.state import AppState, value_analysis_key
from bokucho.webapp.watch import ProjectWatcher

_log = logging.getLogger(__name__)

_CHANGED_OUTSIDE = "This line changed outside Bokucho; choose which version to keep first."

FolderPicker = Callable[[str], str | None]
FilePicker = Callable[[str, str], str | None]
FILE_KINDS = ("game", "dll")
FolderOpener = Callable[[Path], None]
ModelFetcher = Callable[[TranslatorInterfaceConfig, str | None], tuple[str, ...]]


def _wrap_errors(method):
    """Base helper: turn a raised AppError into a JSON-safe error payload."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except AppError as error:
            return {"error": {"kind": error.code, "message": str(error)}}

    return wrapper


def split_api_keys(value: str | None) -> tuple[str, ...]:
    """The keys in one field: one per line, or several separated by commas."""
    if not value:
        return ()
    parts = (part.strip() for line in value.splitlines() for part in line.split(","))
    return tuple(dict.fromkeys(part for part in parts if part))


def _safe_image_path(path: str) -> PurePosixPath:
    """Reject an absolute or traversing image path before any filesystem access."""
    posix = PurePosixPath(path.replace("\\", "/"))
    if (
        not path
        or posix.is_absolute()
        or ".." in posix.parts
        or (posix.parts and ":" in posix.parts[0])
    ):
        raise ValidationFailure(f"Unsafe image path: {path}")
    return posix


def _project_path(path: str, root: Path) -> Path:
    """``path`` as project.toml means it: relative to the project when not absolute."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate


def _font_ref(path: str) -> str:
    """How project.toml names a face: a shipped one by its file, any other by path."""
    return path if path.startswith(BUNDLED_FONT_PREFIX) else Path(path).as_posix()


def _font_file(path: str, root: Path) -> Path:
    """The file a face named in project.toml, or by the image editor, is read from."""
    return _project_path(locate_font(path) or path, root)


def _font_entry(path: str, root: Path) -> dict:
    """A face as the images screen names it; a font file gone missing has no name."""
    return {
        "path": _font_ref(path),
        "name": installed_fonts.font_name(_font_file(path, root)),
    }


def _has_words(unit: TextUnit) -> bool:
    """Whether the queue has anything to show for ``unit``."""
    return unit.kind != "image" or bool(unit.source.strip() or unit.target.strip())


def _category(unit: TextUnit, engine: str) -> str:
    """The queue shelf ``unit`` sits on: a line with a variable in it, else its kind."""
    if any(token["kind"] == "variable" for token in translation_tokens(unit.source, engine)):
        return "variables"
    return {"value": "values", "image": "images"}.get(unit.kind, "story")


class Api:
    """Methods exposed to the frontend as ``window.pywebview.api.<method>``."""

    def __init__(
        self,
        state: AppState,
        events: EventBus,
        *,
        job_runner: JobRunner | None = None,
        registry: dict | None = None,
        credential_store: CredentialStore | None = None,
        folder_picker: FolderPicker | None = None,
        file_picker: FilePicker | None = None,
        folder_opener: FolderOpener | None = None,
        model_fetcher: ModelFetcher | None = None,
        adapter_factory: Callable[[Project], Any] | None = None,
    ) -> None:
        self.state = state
        self.events = events
        self.jobs = job_runner if job_runner is not None else JobRunner(events)
        self.registry = registry if registry is not None else registry_module.JOBS
        self.credential_store = (
            credential_store if credential_store is not None else default_credential_store()
        )
        self._folder_picker = folder_picker
        self._file_picker = file_picker
        self._folder_opener = folder_opener
        self._model_fetcher = model_fetcher or fetch_models_from_provider
        self._adapter_factory: Callable[[Project], Any] = adapter_factory or make_engine_adapter
        self._analysing: tuple | None = None
        self._analysis_lock = threading.Lock()

    def _require_project(self) -> Project:
        if self.state.project is None:
            raise ValidationFailure("No project is open.")
        return self.state.project

    def _engine(self) -> str:
        project = self.state.project
        return project.config.engine.type if project is not None else ""

    def _local_unit(self, source_file: str, unit_id: str) -> TextUnit | None:
        return self.state.find_unit(source_file, unit_id)

    @_wrap_errors
    def choose_directory(self, current_path: str = "") -> str | None:
        if self._folder_picker is None:
            raise ValidationFailure("Native folder picker is unavailable.")
        return self._folder_picker(current_path)

    @_wrap_errors
    def choose_file(self, current_path: str = "", kind: str = "game") -> str | None:
        if kind not in FILE_KINDS:
            raise ValidationFailure(f"No picker for files of kind: {kind}")
        if self._file_picker is None:
            raise ValidationFailure("Native file picker is unavailable.")
        return self._file_picker(current_path, kind)

    @_wrap_errors
    def open_release_folder(self, path: str) -> dict[str, str]:
        project = self._require_project()
        folder = Path(path).resolve()
        releases = (project.root / "dist").resolve()
        if folder.parent != releases or not folder.is_dir():
            raise ValidationFailure("Release folder is unavailable.")
        if self._folder_opener is None:
            raise ValidationFailure("Opening folders is unavailable.")
        try:
            self._folder_opener(folder)
        except OSError as error:
            raise ValidationFailure(f"Could not open release folder: {error}") from error
        return {"opened": str(folder)}

    @staticmethod
    def _unit_row(source_file: str, unit: TextUnit) -> dict:
        regions = unit.engine_ref.get("regions")
        return {
            "source_file": source_file,
            "id": unit.id,
            "source": unit.source,
            "target": unit.target,
            "status": unit.status.value,
            "kind": unit.kind,
            "image": str(unit.engine_ref.get("image", "")) if unit.kind == "image" else "",
            "regions": (
                regions
                if unit.kind == "image" and isinstance(regions, list)
                else []
            ),
        }

    def _unit_matches(
        self,
        source_file: str,
        unit: TextUnit,
        status: str | None,
        needle: str | None,
        scope: str = "source",
        category: str | None = None,
        engine: str = "",
        source_lang: str | None = None,
    ) -> bool:
        """Whether a unit survives the queue's filters."""
        if not _has_words(unit):
            return False
        if status is not None and unit.status.value != status:
            return False
        if category is not None and _category(unit, engine) != category:
            return False
        if source_lang:
            indexed = {
                language
                for language in self.state.source_languages
                if matches_language_tag(language, source_lang)
            }
            if indexed:
                unit_languages = self.state.source_language_index.get(
                    (source_file, unit.id), frozenset()
                )
                if unit_languages.isdisjoint(indexed):
                    return False
            elif not any(
                matches_source_language(line.strip(), source_lang)
                for line in unit.source.splitlines() if line.strip()
            ):
                return False
        if needle is None:
            return True
        field = {"source": unit.source, "target": unit.target, "id": unit.id}[scope]
        return needle in field.casefold()

    @_wrap_errors
    def forget_project(self, path: str) -> list[dict]:
        """Take a workspace off the recent list, leaving the folder alone."""
        self.state.settings.remove_project(path)
        return self.recent_projects()

    @_wrap_errors
    def recent_projects(self) -> list[dict]:
        settings = self.state.settings.load()
        records = []
        for record in settings.recent_projects:
            entry = record.model_dump(mode="json")
            try:
                entry["engine"] = Project.load(record.path).config.engine.type
            except (FileNotFoundError, ValueError):
                entry["engine"] = None
            records.append(entry)
        return records

    @_wrap_errors
    def list_engines(self) -> list[str]:
        return available_engines()

    @_wrap_errors
    def detect_engine(self, path: str) -> dict:
        selected = Path(path)
        if not selected.exists():
            raise ValidationFailure(f"Path does not exist: {path}")
        return {
            "results": [
                {
                    "engine": found.engine,
                    "label": found.label,
                    "confidence": found.confidence,
                    "supported": found.supported,
                    "evidence": found.evidence,
                    "hint": found.hint,
                }
                for found in detect(selected)
            ]
        }

    @_wrap_errors
    def create_project(
        self,
        name: str,
        engine: str | None,
        destination: str,
        source_lang: str = "ja",
        target_lang: str = "zh-tw",
        game: str | None = None,
        force: bool = False,
    ) -> dict:
        game_path = ""
        detected: str | None = None
        warning: str | None = None
        if game:
            selected = Path(game)
            if not selected.exists():
                raise ValidationFailure(f"Path does not exist: {game}")
            game_path = selected.resolve().as_posix()
            supported = [found for found in detect(selected) if found.supported]
            if supported:
                detected = supported[0].engine
                if supported[0].confidence < CONFIDENT:
                    warning = (
                        f"Low-confidence detection ({supported[0].confidence:.2f}) "
                        "-- verify the engine before extracting."
                    )
            elif not engine:
                raise ValidationFailure(
                    "No supported engine found there; pass engine explicitly."
                )

        engine_type = engine or detected or "livemaker"
        project = create_project_use_case(
            CreateProjectRequest(
                destination=Path(destination),
                name=name,
                source_lang=source_lang,
                target_lang=target_lang,
                engine=engine_type,
                force=force,
                game_path=game_path,
            )
        )
        self.state.settings.record_project(project.root)
        result = {"root": str(project.root), "engine": project.config.engine.type}
        if warning:
            result["warning"] = warning
        return result

    @_wrap_errors
    def open_project(self, root: str) -> dict:
        if self.jobs.state is not JobState.IDLE:
            raise JobBusy(f"Wait for {self.jobs.active_name} to finish before opening a project.")
        previous_watcher = self.state.watcher
        self._share_interfaces(root)
        self.state.open(root)
        self.state.pending_conflict = self.state.editing = None
        if previous_watcher is not None:
            previous_watcher.stop()
        self.state.watcher = ProjectWatcher(self.state, self.events)
        self.state.watcher.start()
        summary = load_project_summary(self.state.project.root)
        return {
            "root": str(summary.root),
            "name": summary.name,
            "source_lang": summary.source_lang,
            "target_lang": summary.target_lang,
            "engine": summary.engine,
        }

    @_wrap_errors
    def start_job(self, name: str, options: dict | None = None) -> dict:
        try:
            factory = self.registry[name]
        except KeyError:
            raise ValidationFailure(f"Unknown job: {name}") from None
        if options is not None and not isinstance(options, dict):
            raise ValidationFailure(f"Options for {name} must be an object.")
        if name == "translate" and self.state.project is not None:
            self.jobs.register_secrets(registry_module.translator_secrets(self.state.project))
        try:
            operation = factory(self.state, **(options or {}))
        except TypeError as error:
            raise ValidationFailure(f"{name} does not take those options: {error}") from error
        self.jobs.start(name, operation)
        return {"status": "started", "job": name}

    @_wrap_errors
    def cancel_job(self) -> dict:
        cancelling = self.jobs.cancel()
        return {"status": "cancelling" if cancelling else "idle"}

    @_wrap_errors
    def pause_job(self) -> dict:
        return {"status": "paused" if self.jobs.pause() else "idle"}

    @_wrap_errors
    def resume_job(self) -> dict:
        return {"status": "running" if self.jobs.resume() else "idle"}

    @_wrap_errors
    def get_catalog(self, locale: str) -> dict:
        try:
            return i18n.load_catalog(locale)
        except KeyError as error:
            return {"error": {"kind": "not-found", "message": str(error)}}

    @_wrap_errors
    def unit_rows(
        self,
        status: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 200,
        scope: str = "source",
        category: str | None = None,
        source_lang: str | None = None,
    ) -> dict:
        needle = q.casefold() if q else None
        engine = self._engine()
        first = max(0, offset)
        last = first + max(0, limit)
        total = 0
        rows = []
        for string_file in self.state.string_files:
            for unit in string_file.units:
                if not self._unit_matches(
                    string_file.source_file,
                    unit, status, needle, scope, category, engine, source_lang,
                ):
                    continue
                if first <= total < last:
                    rows.append(self._unit_row(string_file.source_file, unit))
                total += 1
        return {"total": total, "rows": rows}

    @_wrap_errors
    def unit_languages(self) -> list[str]:
        return self.state.source_languages

    @_wrap_errors
    def quality(self) -> dict:
        """The current findings, checked against what is in memory right now."""
        project = self._require_project()
        report = qa_report(project, self.state.string_files)
        values = self._value_issues(project)
        return {
            "status_counts": {
                status.value: count for status, count in report.status_counts.items()
            },
            "issues": [asdict(issue) for issue in (*report.issues, *values)],
            "passed": report.passed and not values,
        }

    def _value_issues(self, project: Project) -> list[Issue]:
        """Values a LiveMaker game derives that the translations would no longer match.

        Reading the game for them takes every script -- half a minute on a large
        one -- and what it finds changes only with the game, so it is read once,
        beside the GUI, and the screen is told to check again when it lands.
        """
        if value_analysis_key(project) is None:
            return []
        values = self.state.analysed_values()
        if values is None:
            if not self._analysed_now(project):
                self._analyse_values(project)
            return []
        files = self.state.string_files
        owners = {unit.id: f.source_file for f in files for unit in f.units if unit.kind == "value"}
        units = [unit for f in files for unit in f.units if not unit.missing]
        return [
            Issue.of(owners.get(unit_id, "values"), unit_id, finding)
            for unit_id, finding in self._adapter_factory(project).value_problems(values, units)
        ]

    def _analysed_now(self, project: Project) -> bool:
        """Whether the analysis on record was made from the game as it is, even if it failed."""
        return self.state.value_analysis is not None and (
            self.state.value_analysis[0] == value_analysis_key(project)
        )

    def _analyse_values(self, project: Project) -> None:
        key = value_analysis_key(project)
        with self._analysis_lock:
            if self._analysing == key:
                return
            self._analysing = key
        adapter = self._adapter_factory(project)
        game = resolve_game_path(project)

        def run() -> None:
            try:
                values = adapter.analyse_values(game)
            except Exception:
                # Beside the GUI there is no one to raise to; the checks that
                # need it are left out, and inject still refuses on its own.
                _log.exception("Could not read the values of %s", game)
                values = None
            self.state.value_analysis = (key, values)
            with self._analysis_lock:
                if self._analysing == key:
                    self._analysing = None
            self.events.push({"type": "values-analysed"})

        threading.Thread(target=run, name="value-analysis", daemon=True).start()

    @_wrap_errors
    def recheck_preview(self) -> dict:
        """How many machine drafts the ``recheck`` job would put right or send back."""
        project = self._require_project()
        return asdict(recheck_report(project, self.state.string_files))

    @_wrap_errors
    def brackets_preview(self) -> dict:
        """How many messages the ``fix-brackets`` job would put right."""
        project = self._require_project()
        return asdict(bracket_fix_report(project, self.state.string_files))

    @_wrap_errors
    def values_preview(self) -> dict:
        """How many values the ``fix-values`` job would assemble from their parts."""
        project = self._require_project()
        values = self.state.analysed_values()
        if values is None:
            raise ValidationFailure("The game's values are still being read.")
        units = [unit for f in self.state.string_files for unit in f.units if not unit.missing]
        return {"fixed": len(self._adapter_factory(project).assemble_values(values, units))}

    @_wrap_errors
    def unit_counts(self) -> dict:
        """Status counts of what the queue lists, which leaves out a picture with no text on it."""
        counts = {status.value: 0 for status in Status}
        files = []
        pictures = 0
        for string_file in self.state.string_files:
            per_file = {status.value: 0 for status in Status}
            for unit in string_file.units:
                pictures += unit.kind == "image"
                if not _has_words(unit):
                    continue
                counts[unit.status.value] += 1
                per_file[unit.status.value] += 1
            files.append(
                {
                    "source_file": string_file.source_file,
                    "total": sum(per_file.values()),
                    "status_counts": per_file,
                }
            )
        return {
            "total": sum(counts.values()),
            "status_counts": counts,
            "files": files,
            "image_units": pictures,
        }

    @_wrap_errors
    def next_unit(
        self,
        source_file: str,
        id: str,
        status: str | None = None,
        q: str | None = None,
        scope: str = "source",
        category: str | None = None,
        source_lang: str | None = None,
    ) -> dict | None:
        ordered = [
            (string_file.source_file, unit)
            for string_file in self.state.string_files
            for unit in string_file.units
        ]
        try:
            current_index = next(
                index
                for index, (owner, unit) in enumerate(ordered)
                if owner == source_file and unit.id == id
            )
        except StopIteration:
            raise ValidationFailure(f"Text unit not found: {source_file} {id}") from None

        needle = q.casefold() if q else None
        engine = self._engine()
        for owner, unit in ordered[current_index + 1 :]:
            if self._unit_matches(
                owner, unit, status, needle, scope, category, engine, source_lang,
            ):
                return self._unit_row(owner, unit)
        return None

    @_wrap_errors
    def unit_detail(self, source_file: str, id: str) -> dict:
        unit = self._local_unit(source_file, id)
        if unit is None:
            raise ValidationFailure(f"Text unit not found: {source_file} {id}")
        self._require_project()
        memory = self.state.memory if self.state.memory is not None else TranslationMemory([])
        suggestions = suggestions_for(
            memory, self.state.glossary_mapping, unit.source, unit.target or None
        )
        return {
            **self._unit_row(source_file, unit),
            "scene": unit.context.scene or "",
            "suggestions": [
                {
                    "target": suggestion.target,
                    "origin": suggestion.origin,
                    "status": suggestion.status.value if suggestion.status else None,
                    "count": suggestion.count,
                }
                for suggestion in suggestions
            ],
            "history": [entry.model_dump(mode="json") for entry in unit.history],
            "spans": [
                [start, end, self.state.glossary_mapping[unit.source[start:end]]]
                for start, end in glossary_spans(self.state.glossary_mapping, unit.source)
            ],
        }

    def _refuse_an_outside_change(
        self, path: Path, source_file: str, unit_id: str, target: str, baseline: str
    ) -> None:
        try:
            unit = load_string_file(path).by_id(unit_id)
        except (OSError, ValueError):
            return
        if unit is None or unit.target == baseline:
            return
        self.state.pending_conflict = {
            "source_file": source_file,
            "unit_id": unit_id,
            "disk_target": unit.target,
            "draft_target": target,
        }
        self.events.push({"type": "conflict", "file": source_file, "unit_id": unit_id})
        raise ValidationFailure(_CHANGED_OUTSIDE)

    @_wrap_errors
    def save_draft(
        self, source_file: str, id: str, target: str, baseline: str | None = None
    ) -> dict:
        """Save ``target``, unless the line moved away from ``baseline`` outside Bokucho."""
        project = self._require_project()
        conflict = self.state.pending_conflict
        if conflict is not None and (conflict["source_file"], conflict["unit_id"]) == (
            source_file,
            id,
        ):
            raise ValidationFailure(_CHANGED_OUTSIDE)
        path = project.strings_dir / string_filename(source_file)
        with file_lock(path):
            if baseline is not None and not written_here(path):
                self._refuse_an_outside_change(path, source_file, id, target, baseline)
            try:
                saved = save_target(project, source_file, id, target)
            except OverwriteRefused as error:
                raise ValidationFailure(_CHANGED_OUTSIDE) from error
        local = self._local_unit(source_file, id)
        if local is not None:
            local.target = saved.target
            local.status = saved.status
        self.state.settle_edit(source_file, id, saved.target)
        return self._unit_row(source_file, saved)

    @_wrap_errors
    def begin_edit(
        self, source_file: str, id: str, target: str, baseline: str | None = None
    ) -> dict:
        self._require_project()
        self.state.begin_edit(source_file, id, target, baseline)
        return {"status": "ok"}

    @_wrap_errors
    def accept_disk(self) -> dict:
        self._require_project()
        if self.state.pending_conflict is None:
            raise ValidationFailure("No pending conflict to resolve.")
        self.state.pending_conflict = self.state.editing = None
        self.state.reload()
        return {"status": "ok"}

    @_wrap_errors
    def keep_draft(self) -> dict:
        project = self._require_project()
        conflict = self.state.pending_conflict
        if conflict is None:
            raise ValidationFailure("No pending conflict to resolve.")
        editing = self.state.editing
        draft = (
            editing[2]
            if editing is not None
            and editing[:2] == (conflict["source_file"], conflict["unit_id"])
            else conflict["draft_target"]
        )
        self.state.pending_conflict = self.state.editing = None
        try:
            save_target(project, conflict["source_file"], conflict["unit_id"], draft)
        except Exception:
            self.state.pending_conflict, self.state.editing = conflict, editing
            raise
        self.state.reload()
        return {"status": "ok"}

    @_wrap_errors
    def mark_reviewed(self, source_file: str, id: str) -> dict:
        project = self._require_project()
        saved = mark_unit_reviewed(project, source_file, id)
        local = self._local_unit(source_file, id)
        if local is not None:
            local.target = saved.target
            local.status = saved.status
            local.history = saved.history
        return self._unit_row(source_file, saved)

    @_wrap_errors
    def set_unit_status(self, source_file: str, id: str, status: str) -> dict:
        project = self._require_project()
        saved = set_status_use_case(project, source_file, id, Status(status))
        local = self._local_unit(source_file, id)
        if local is not None:
            local.target = saved.target
            local.status = saved.status
            local.history = saved.history
        return self._unit_row(source_file, saved)

    @_wrap_errors
    def import_mapping(self, mapping: dict) -> dict:
        project = self._require_project()
        result = import_translations(project, mapping)
        self.state.reload()
        return {"imported": result.imported, "unmatched": result.unmatched}

    @_wrap_errors
    def prompt_presets(self) -> dict:
        project = self.state.project
        return {
            "presets": [preset.model_dump(mode="json") for preset in load_prompt_library()],
            "selected_id": project.config.prompts.preset_id if project else "",
        }

    @_wrap_errors
    def save_prompt_preset(self, values: dict) -> dict:
        try:
            preset = PromptPreset.model_validate({**values, "builtin": False})
            presets = save_prompt_preset(preset)
        except ValueError as error:
            raise ValidationFailure(str(error)) from error
        return {"presets": [item.model_dump(mode="json") for item in presets]}

    @_wrap_errors
    def delete_prompt_preset(self, preset_id: str) -> dict:
        try:
            presets = delete_prompt_preset(preset_id)
        except ValueError as error:
            raise ValidationFailure(str(error)) from error
        project = self.state.project
        if project is not None and project.config.prompts.preset_id == preset_id:
            self._select_preset(project, "")
        return {"presets": [item.model_dump(mode="json") for item in presets]}

    @_wrap_errors
    def select_prompt_preset(self, preset_id: str) -> dict:
        project = self._require_project()
        if preset_id and find_prompt_preset(preset_id) is None:
            raise ValidationFailure(f"Unknown prompt preset: {preset_id}")
        self._select_preset(project, preset_id)
        return {"selected_id": preset_id}

    def _select_preset(self, project: Project, preset_id: str) -> None:
        data = project.config.model_dump()
        data["prompts"] = {**data["prompts"], "preset_id": preset_id}
        self._save_config(project, data)

    @_wrap_errors
    def glossary_entries(self) -> list[dict]:
        project = self._require_project()
        occurrences = glossary_occurrences(project, self.state.string_files)
        return [
            {
                "source": entry.source,
                "target": entry.target,
                "note": entry.note,
                "origin": origin,
                "occurrences": occurrences.get(entry.source, 0),
            }
            for entry, origin in list_glossary_entries(project)
        ]

    @_wrap_errors
    def save_glossary_entry(
        self,
        source: str,
        target: str,
        note: str | None = None,
        scope: str = "project",
    ) -> dict:
        project = self._require_project()
        created = upsert_glossary(project, source, target, note, scope=scope)
        self.state.refresh_glossary()
        return {"created": created}

    @_wrap_errors
    def delete_glossary_entry(self, source: str, scope: str = "project") -> dict:
        project = self._require_project()
        deleted = delete_glossary(project, source, scope=scope)
        self.state.refresh_glossary()
        return {"deleted": deleted}

    @_wrap_errors
    def ocr_status(self) -> dict:
        settings = self.state.settings.load()

        def checkpoint_status(checkpoint) -> dict:
            return {
                "model_id": checkpoint.model_id,
                "revision": checkpoint.revision,
                "download_bytes": checkpoint.download_bytes,
                "cached": images_mod.ocr_model_cached(checkpoint),
            }

        return {
            "detector": checkpoint_status(DETECTOR_CHECKPOINT),
            "recognizer": checkpoint_status(RECOGNIZER_CHECKPOINT),
            "detection_threshold": settings.ocr_detection_threshold,
            "recognition_threshold": settings.ocr_recognition_threshold,
            "batch_size": settings.ocr_batch_size or DEFAULT_BATCH_SIZE,
            "recommended_batch_size": DEFAULT_BATCH_SIZE,
            "batch_size_mode": (
                "custom" if settings.ocr_batch_size is not None else "auto"
            ),
            "eraser": {
                "download_bytes": render_mod.MODEL_BYTES,
                "cached": images_mod.eraser_model_cached(),
            },
            "runtime_cached": images_mod.portable_ocr_runtime_cached(),
            "runtime_download_bytes": images_mod.CUDA_RUNTIME_DOWNLOAD_BYTES,
        }

    @_wrap_errors
    def save_ocr_configuration(
        self,
        batch_size: int,
        detection_threshold: float,
        recognition_threshold: float,
    ) -> dict:
        if not 0 <= batch_size <= 64:
            raise ValidationFailure("OCR batch size must be 0 or between 1 and 64.")
        try:
            checked = GuiSettings(
                ocr_batch_size=batch_size or None,
                ocr_detection_threshold=detection_threshold,
                ocr_recognition_threshold=recognition_threshold,
            )
        except ValidationError as error:
            raise ValidationFailure(
                "OCR confidence thresholds must be numbers between 0 and 1."
            ) from error
        self.state.settings.update_ocr_configuration(
            checked.ocr_batch_size,
            checked.ocr_detection_threshold,
            checked.ocr_recognition_threshold,
        )
        return self.ocr_status()

    @_wrap_errors
    def image_inventory(self) -> dict:
        project = self._require_project()
        units = load_image_units(project).units
        regions = {
            str(unit.engine_ref.get("image")): unit.engine_ref["regions"]
            for unit in units
            if unit.engine_ref.get("regions")
        }
        ignored = {
            str(unit.engine_ref.get("image")) for unit in units if unit.status is Status.IGNORED
        }
        entries = []
        for entry in load_inventory(project):
            image = str(entry.get("image"))
            listed = dict(entry)
            if located := regions.get(image):
                listed["regions"] = located
            if image in ignored:
                listed["ignored"] = True
            entries.append(listed)
        return {"entries": entries}

    @_wrap_errors
    def set_images_ignored(self, paths: list[str], ignored: bool) -> dict:
        project = self._require_project()
        images = [_safe_image_path(path).as_posix() for path in paths]
        changed = set_images_ignored(project, images, ignored)
        self.state.reload()
        return {"changed": changed}

    @_wrap_errors
    def typeset_font(self) -> dict:
        """The face pictures are typeset in if one was chosen, and the default."""
        project = self._require_project()
        chosen = project.config.images.font
        default = render_mod.resolve_font(None, project.root)
        return {
            "font": None if chosen is None else _font_entry(chosen, project.root),
            "default": None if default is None else _font_entry(default, project.root),
        }

    @_wrap_errors
    def typeset_fonts(self) -> dict:
        """The faces that draw everything the pictures are translated as."""
        project = self._require_project()
        text = "".join(
            unit.target
            for unit in load_image_units(project).units
            if unit.target and unit.status is not Status.IGNORED
        )
        if not text.strip():
            return {"fonts": []}
        fonts = [
            *installed_fonts.bundled_fonts_covering(text),
            *installed_fonts.fonts_covering(text),
        ]
        return {"fonts": [asdict(font) for font in fonts]}

    @_wrap_errors
    def save_typeset_font(self, path: str | None) -> dict:
        """Typeset the pictures in the face at ``path``, or in the default for None."""
        project = self._require_project()
        if path and installed_fonts.font_name(_font_file(path, project.root)) is None:
            raise ValidationFailure(f"Not a font that can be typeset with: {path}")
        data = project.config.model_dump()
        data["images"]["font"] = _font_ref(path) if path else None
        data["images"]["variation"] = ""
        self._save_config(project, data)
        return self.typeset_font()

    @_wrap_errors
    def typeset_font_data(self, path: str) -> dict:
        """The file of the face the image editor shows a text layer in."""
        project = self._require_project()
        file = _font_file(path, project.root)
        if installed_fonts.font_name(file) is None:
            raise ValidationFailure(f"Not a font that can be typeset with: {path}")
        return {"data": base64.b64encode(file.read_bytes()).decode("ascii")}

    @_wrap_errors
    def game_font(self) -> dict:
        """The face the game's own font is redrawn from, if one was chosen."""
        project = self._require_project()
        sources = project.config.font.sources
        return {
            "font": _font_entry(sources[0].path, project.root) if sources else None,
            "sample": sample_lines(project),
        }

    @_wrap_errors
    def game_fonts(self) -> dict:
        """The installed faces that draw every character of the translated lines."""
        project = self._require_project()
        text = required_characters(project)
        return {"fonts": [asdict(font) for font in installed_fonts.fonts_covering(text)]}

    @_wrap_errors
    def save_game_font(self, path: str) -> dict:
        """Draw the game text from the face at ``path`` first."""
        project = self._require_project()
        if not path or installed_fonts.font_name(_project_path(path, project.root)) is None:
            raise ValidationFailure(f"Not a font the game text can be drawn from: {path}")
        chosen = Path(path).as_posix()
        data = project.config.model_dump()
        others = [source for source in data["font"]["sources"] if source["path"] != chosen]
        data["font"]["sources"] = [{"path": chosen, "variation": ""}, *others]
        self._save_config(project, data)
        return self.game_font()

    @_wrap_errors
    def uif_dll(self) -> dict:
        """UIF's DLL the patch puts beside a game that draws with a system face."""
        project = self._require_project()
        chosen = project.config.engine.uif_dll
        return {
            "path": Path(chosen).as_posix() if chosen else None,
            "bundled": bundled_uif() is not None,
        }

    @_wrap_errors
    def save_uif_dll(self, path: str | None) -> dict:
        """Keep the translator's UIF ``winmm.dll``, or forget it for None."""
        project = self._require_project()
        if path:
            file = _project_path(path, project.root)
            if (
                file.suffix.lower() != ".dll"
                or not file.is_file()
                or file.read_bytes()[:2] != b"MZ"
            ):
                raise ValidationFailure(f"Not a DLL: {path}")
            if file.name.lower() != UIF_DLL:
                raise ValidationFailure(
                    f"LiveMaker loads UIF as {UIF_DLL}, not {file.name}: choose UIF's {UIF_DLL}."
                )
            if problem := uif_problem(file.read_bytes()):
                raise ValidationFailure(f"{path} is {problem}: choose UIF's {UIF_DLL}.")
        data = project.config.model_dump()
        data["engine"]["uif_dll"] = Path(path).as_posix() if path else None
        self._save_config(project, data)
        return self.uif_dll()

    @_wrap_errors
    def image_data(self, path: str) -> dict:
        project = self._require_project()
        relative = _safe_image_path(path)
        document = load_image_document(project, relative.as_posix(), make_image_codec())
        return {"data": base64.b64encode(document.image_bytes).decode("ascii")}

    @_wrap_errors
    def image_editor_state(self, path: str) -> dict:
        project = self._require_project()
        relative = _safe_image_path(path)
        return load_editor_state(project, relative.as_posix(), make_image_codec())

    @_wrap_errors
    def save_image_editor_state(
        self,
        path: str,
        source_fingerprint: str,
        state: dict,
    ) -> dict:
        project = self._require_project()
        relative = _safe_image_path(path)
        result = save_editor_state(
            project,
            relative.as_posix(),
            source_fingerprint,
            state,
            make_image_codec(),
        )
        self.state.reload()
        return result

    @_wrap_errors
    def gui_settings(self) -> dict:
        return self.state.settings.load().model_dump(mode="json")

    @_wrap_errors
    def save_gui_settings(
        self,
        theme: str | None = None,
        accent: str | None = None,
        density: str | None = None,
        locale: str | None = None,
        nav_expanded: bool | None = None,
        font_family: str | None = None,
        font_size: int | None = None,
    ) -> dict:
        data = self.state.settings.load().model_dump()
        updates = {
            "theme": theme,
            "accent": accent,
            "density": density,
            "locale": locale,
            "nav_expanded": nav_expanded,
            "font_family": font_family,
            "font_size": font_size,
        }
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        settings = GuiSettings.model_validate(data)
        self.state.settings.save(settings)
        return settings.model_dump(mode="json")

    @_wrap_errors
    def font_families(self) -> dict:
        """The installed font families the interface can be set in."""
        return {"families": installed_fonts.font_families()}

    @_wrap_errors
    def get_settings(self) -> dict:
        project = self._require_project()
        return project.config.model_dump(mode="json")

    @_wrap_errors
    def save_project_settings(
        self,
        name: str | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,
        game_path: str | None = None,
    ) -> dict:
        project = self._require_project()
        data = project.config.model_dump()
        updates = {"name": name, "source_lang": source_lang, "target_lang": target_lang}
        for key, value in updates.items():
            if value is not None:
                data[key] = value
        if game_path is not None:
            normalized_game_path = ""
            if game_path.strip():
                selected = Path(game_path).expanduser()
                if not selected.is_absolute():
                    selected = project.root / selected
                if not selected.exists():
                    raise ValidationFailure(f"Path does not exist: {game_path}")
                normalized_game_path = selected.resolve().as_posix()
            data["engine"]["game_path"] = normalized_game_path
        return self._save_config(project, data)

    @_wrap_errors
    def save_translator_settings(
        self,
        provider: str | None = None,
        model: str | None = None,
        batch_size: int | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        request_options: dict | None = None,
    ) -> dict:
        project = self._require_project()
        translator = project.config.translator.model_dump()
        updates = {
            "provider": provider,
            "model": model,
            "batch_size": batch_size,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "request_options": request_options,
        }
        for key, value in updates.items():
            if value is not None:
                translator[key] = value
        data = project.config.model_dump()
        data["translator"] = translator
        return self._save_config(project, data)

    @_wrap_errors
    def provider_interfaces(self) -> dict:
        project = self._require_project()
        translator = project.config.translator
        interfaces = self._interface_catalog(translator)
        active_id = translator.active_interface_id or interfaces[0].id
        return {
            "providers": [
                {
                    "key": preset.key,
                    "label": preset.label,
                    "base_url": preset.base_url,
                    "api_key_env": preset.api_key_env,
                }
                for preset in PROVIDER_PRESETS
            ],
            "interfaces": [
                {
                    **interface.model_dump(mode="json"),
                    "credential_count": len(self._interface_secrets(interface)),
                }
                for interface in interfaces
            ],
            "active_interface_id": active_id,
        }

    @_wrap_errors
    def provider_interface_keys(self, interface_id: str) -> dict:
        """Return stored keys only when one interface is explicitly edited."""
        interface = self._find_interface(interface_id)
        secrets = self._interface_secrets(interface)
        self.jobs.register_secrets(secrets)
        return {"api_keys": list(secrets)}

    @_wrap_errors
    def save_provider_interface(
        self,
        values: dict,
        api_key: str | None = None,
    ) -> dict:
        project = self._require_project()
        translator, legacy_id = self._materialize_translator(
            project.config.translator
        )
        interfaces = list(translator.interfaces)
        requested_id = str(values.get("id") or "").strip()
        interface_id = legacy_id if requested_id == "legacy" else requested_id
        existing = next(
            (item for item in interfaces if item.id == interface_id),
            None,
        )
        shared = None if existing is not None else self._shared_interface(interface_id)
        base = existing or shared
        if base is not None:
            self._interface_secrets(base)
        is_new = base is None
        provider = str(values.get("provider") or (base.provider if base else "")).strip()
        try:
            preset = get_provider_preset(provider)
        except ValueError as error:
            raise ValidationFailure(str(error)) from error

        payload = base.model_dump() if base is not None else {}
        payload.update(values)
        payload["id"] = interface_id or uuid4().hex
        payload["provider"] = provider
        payload.setdefault("name", preset.label)
        payload.setdefault("model", "")
        if payload.get("base_url") is None:
            payload["base_url"] = preset.base_url
        payload["api_key_env"] = ""
        interface = self._validate_interface(payload)

        if shared is None:
            if existing is None:
                interfaces.append(interface)
            else:
                interfaces[interfaces.index(existing)] = interface
            active_id = (
                interface.id
                if is_new and translator.active_interface_id is None
                else translator.active_interface_id
            )
            self._save_interfaces(project, translator, interfaces, active_id)
        self.state.settings.remember_interface(interface)

        keys = split_api_keys(api_key)
        if base is not None:
            keys = self._keys_left_home(keys, base, interface)
        if keys:
            scope = self._interface_endpoint(interface)
            for key in keys:
                self.credential_store.add(interface.provider, scope, key)
            self.jobs.register_secrets(tuple(keys))
        return {"interface": self._interface_payload(interface)}

    @_wrap_errors
    def clear_provider_interface_keys(self, interface_id: str) -> dict:
        """Forget keys shared by this interface's provider and endpoint."""
        interface = self._find_interface(interface_id)
        self._interface_secrets(interface)
        self.credential_store.delete(
            interface.provider,
            self._interface_endpoint(interface),
        )
        return {"interface": self._interface_payload(interface)}

    @_wrap_errors
    def activate_provider_interface(self, interface_id: str) -> dict:
        project = self._require_project()
        translator, legacy_id = self._materialize_translator(
            project.config.translator
        )
        resolved_id = legacy_id if interface_id == "legacy" else interface_id
        interfaces = list(translator.interfaces)
        if not any(item.id == resolved_id for item in interfaces):
            shared = self._shared_interface(resolved_id)
            if shared is None:
                raise ValidationFailure(f"Unknown translator interface: {interface_id}")
            interfaces.append(shared)
        self._save_interfaces(project, translator, interfaces, resolved_id)
        return {"active_interface_id": resolved_id}

    @_wrap_errors
    def update_provider_interface_parameters(
        self,
        interface_id: str,
        rpm: int,
        tpm: int,
        rpd: int,
        temperature: float | None = None,
        thinking_level: str | None = None,
        batch_size: int | None = None,
        max_request_tokens: int | None = None,
        context_units: int | None = None,
        concurrency: int | None = None,
    ) -> dict:
        project = self._require_project()
        translator, legacy_id = self._materialize_translator(
            project.config.translator
        )
        resolved_id = legacy_id if interface_id == "legacy" else interface_id
        interfaces = list(translator.interfaces)
        existing = next(
            (item for item in interfaces if item.id == resolved_id),
            None,
        ) or self._shared_interface(resolved_id)
        if existing is None:
            raise ValidationFailure(f"Unknown translator interface: {interface_id}")
        kept_unless_given = {
            "batch_size": batch_size,
            "max_request_tokens": max_request_tokens,
            "context_units": context_units,
            "concurrency": concurrency,
        }
        interface = self._validate_interface(
            {
                **existing.model_dump(),
                "rpm": rpm,
                "tpm": tpm,
                "rpd": rpd,
                "temperature": temperature,
                "thinking_level": thinking_level,
                **{key: value for key, value in kept_unless_given.items() if value is not None},
            }
        )
        if existing in interfaces:
            interfaces[interfaces.index(existing)] = interface
            self._save_interfaces(
                project, translator, interfaces, translator.active_interface_id
            )
        self.state.settings.remember_interface(interface)
        return {"interface": self._interface_payload(interface)}

    @_wrap_errors
    def delete_provider_interface(self, interface_id: str) -> dict:
        project = self._require_project()
        translator, legacy_id = self._materialize_translator(
            project.config.translator
        )
        resolved_id = legacy_id if interface_id == "legacy" else interface_id
        interfaces = [item for item in translator.interfaces if item.id != resolved_id]
        deleted = next(
            (item for item in translator.interfaces if item.id == resolved_id),
            None,
        ) or self._shared_interface(resolved_id)
        if deleted is None:
            raise ValidationFailure(f"Unknown translator interface: {interface_id}")
        active_id = translator.active_interface_id
        if len(interfaces) != len(translator.interfaces):
            if not interfaces:
                raise ValidationFailure("At least one translator interface must remain.")
            if active_id == resolved_id:
                active_id = interfaces[0].id
            self._save_interfaces(project, translator, interfaces, active_id)
        self.state.settings.forget_interface(resolved_id)
        self.credential_store.delete(
            deleted.provider,
            interface_credential_scope(deleted.id),
        )
        return {"deleted": resolved_id, "active_interface_id": active_id}

    @_wrap_errors
    def discover_provider_models(
        self,
        provider: str,
        base_url: str | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
        interface_id: str | None = None,
    ) -> dict:
        """List a provider's models from connection details still being edited."""
        try:
            preset = get_provider_preset(provider)
        except ValueError as error:
            raise ValidationFailure(str(error)) from error
        interface = self._validate_interface(
            {
                "id": "model-discovery",
                "name": preset.label,
                "provider": provider,
                "model": "model-discovery",
                "base_url": base_url or preset.base_url,
                "api_key_env": api_key_env or preset.api_key_env,
            }
        )
        secret = api_key
        stored = self._find_interface(interface_id) if interface_id else None
        if secret and stored is not None:
            secret = next(iter(self._keys_left_home((secret,), stored, interface)), None)
        if secret:
            self.jobs.register_secrets((secret,))
        elif stored is not None:
            if self._interface_account(stored) == self._interface_account(interface):
                secret = next(iter(self._interface_secrets(stored)), None)
        return {"models": list(self._model_fetcher(interface, secret))}

    @_wrap_errors
    def test_provider_interface(self, interface_id: str) -> dict:
        interface = self._find_interface(interface_id)
        secret = next(
            iter(self._interface_secrets(interface)),
            None,
        )
        models = self._model_fetcher(interface, secret)
        return {"ok": True, "models": len(models)}

    def _materialize_translator(
        self,
        translator: TranslatorConfig,
    ) -> tuple[TranslatorConfig, str | None]:
        if translator.interfaces:
            return translator, None

        legacy = translator.selected_interface()
        interface_id = uuid4().hex
        migrated = legacy.model_copy(
            update={
                "id": interface_id,
                "name": get_provider_preset(legacy.provider).label,
            }
        )
        materialized = TranslatorConfig.model_validate(
            {
                **translator.model_dump(),
                "interfaces": [migrated.model_dump()],
                "active_interface_id": interface_id,
            }
        )
        return materialized, interface_id

    def _save_interfaces(
        self,
        project: Project,
        translator: TranslatorConfig,
        interfaces: list[TranslatorInterfaceConfig],
        active_id: str | None,
    ) -> None:
        updated = TranslatorConfig.model_validate(
            {
                **translator.model_dump(),
                "interfaces": [item.model_dump() for item in interfaces],
                "active_interface_id": active_id,
            }
        )
        data = project.config.model_dump()
        data["translator"] = updated.model_dump()
        self._save_config(project, data)

    def _find_interface(self, interface_id: str) -> TranslatorInterfaceConfig:
        translator = self._require_project().config.translator
        interface = next(
            (
                item
                for item in self._interface_catalog(translator)
                if item.id == interface_id
            ),
            None,
        )
        if interface is None:
            raise ValidationFailure(f"Unknown translator interface: {interface_id}")
        return interface

    def _interface_catalog(
        self,
        translator: TranslatorConfig,
    ) -> list[TranslatorInterfaceConfig]:
        """This project's interfaces, then the library's it does not use yet."""
        library = self.state.settings.load().interfaces
        shared = {item.id: item for item in library}
        own = (
            list(translator.interfaces)
            if translator.interfaces
            else [translator.selected_interface()]
        )
        own_ids = {item.id for item in own}
        return [
            *(shared.get(item.id, item) for item in own),
            *(item for item in library if item.id not in own_ids),
        ]

    def _shared_interface(self, interface_id: str) -> TranslatorInterfaceConfig | None:
        return next(
            (
                item
                for item in self.state.settings.load().interfaces
                if item.id == interface_id
            ),
            None,
        )

    def _connection(self, interface: TranslatorInterfaceConfig) -> tuple:
        return (interface.provider, self._interface_endpoint(interface), interface.model)

    def _share_interfaces(self, root: str) -> None:
        """Line a project's interfaces up with the library before it opens."""
        try:
            project = Project.load(root)
        except (FileNotFoundError, ValueError):
            return
        translator = project.config.translator
        if not translator.interfaces:
            return
        settings = self.state.settings.load()
        library = {item.id: item for item in settings.interfaces}
        by_connection: dict[tuple, TranslatorInterfaceConfig] = {}
        for item in settings.interfaces:
            by_connection.setdefault(self._connection(item), item)
        adopted = False
        active_id = translator.active_interface_id
        lined_up: dict[str, TranslatorInterfaceConfig] = {}
        for own in translator.interfaces:
            if own.id in library:
                item = library[own.id]
            elif own.id in settings.deleted_interfaces:
                item = own
            elif (match := by_connection.get(self._connection(own))) is not None:
                self._interface_secrets(own)
                item = match
                if active_id == own.id:
                    active_id = match.id
            else:
                item = library[own.id] = own
                by_connection.setdefault(self._connection(own), own)
                settings.interfaces.append(own)
                adopted = True
            lined_up.setdefault(item.id, item)
        if adopted:
            self.state.settings.save(settings)
        interfaces = list(lined_up.values())
        if interfaces != translator.interfaces or active_id != translator.active_interface_id:
            updated = TranslatorConfig.model_validate(
                {
                    **translator.model_dump(),
                    "interfaces": [item.model_dump() for item in interfaces],
                    "active_interface_id": active_id,
                }
            )
            project.save_config(project.config.model_copy(update={"translator": updated}))

    def _interface_secrets(
        self,
        interface: TranslatorInterfaceConfig,
    ) -> tuple[str, ...]:
        return shared_provider_credentials(
            self.credential_store,
            interface.provider,
            self._interface_endpoint(interface),
            interface.id,
        )

    @staticmethod
    def _interface_endpoint(interface: TranslatorInterfaceConfig) -> str | None:
        return interface.base_url or provider_default_base_url(interface.provider)

    def _interface_account(self, interface: TranslatorInterfaceConfig) -> str:
        return credential_account(interface.provider, self._interface_endpoint(interface))

    def _keys_left_home(
        self,
        keys: Sequence[str],
        stored: TranslatorInterfaceConfig,
        target: TranslatorInterfaceConfig,
    ) -> list[str]:
        """``keys`` less those held for ``stored``'s address, when ``target`` is elsewhere."""
        if self._interface_account(stored) == self._interface_account(target):
            return list(keys)
        home = set(self._interface_secrets(stored))
        return [key for key in keys if key not in home]

    def _interface_payload(self, interface: TranslatorInterfaceConfig) -> dict:
        return {
            **interface.model_dump(mode="json"),
            "credential_count": len(self._interface_secrets(interface)),
        }

    @staticmethod
    def _validate_interface(values: dict) -> TranslatorInterfaceConfig:
        try:
            return TranslatorInterfaceConfig.model_validate(values)
        except (ValueError, TypeError) as error:
            raise ValidationFailure(str(error)) from error

    def _save_config(self, project: Project, data: dict) -> dict:
        try:
            updated_config = ProjectConfig.model_validate(data)
        except (ValueError, TypeError) as error:
            raise ValidationFailure(str(error)) from error
        updated = project.save_config(updated_config)
        self.state.reload()
        return updated.model_dump(mode="json")
