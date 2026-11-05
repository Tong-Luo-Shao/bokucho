"""In-process application state for the webapp bridge."""

from __future__ import annotations

from pathlib import Path

from bokucho.application.errors import InvalidProject
from bokucho.application.projects import resolve_game_path, string_filename
from bokucho.application.suggestions import TranslationMemory
from bokucho.language import source_language_index
from bokucho.model import StringFile, TextUnit
from bokucho.project import Project
from bokucho.sync.atomic import file_lock
from bokucho.sync.json_io import load_string_file
from bokucho.translate.glossary import Glossary
from bokucho.user_settings import UserSettingsStore


def value_analysis_key(project: Project) -> tuple | None:
    """What a LiveMaker game's value analysis is made from; it holds while the game file does."""
    if project.config.engine.type != "livemaker":
        return None
    try:
        game = resolve_game_path(project)
        stat = game.stat()
    except (InvalidProject, OSError):
        return None
    return (str(project.root), str(game), stat.st_mtime_ns, stat.st_size)


class AppState:
    """Everything the bridge knows about the currently open project, if any."""

    def __init__(self, settings: UserSettingsStore | None = None) -> None:
        self.project: Project | None = None
        self.string_files: list[StringFile] = []
        self.source_languages: list[str] = []
        self.source_language_index: dict[tuple[str, str], frozenset[str]] = {}
        self.memory: TranslationMemory | None = None
        self.glossary_mapping: dict[str, str] = {}
        self.settings: UserSettingsStore = settings if settings is not None else UserSettingsStore()
        self.watcher: object | None = None
        self.pending_conflict: dict[str, object] | None = None
        self.editing: tuple[str, str, str, str] | None = None
        #: (what it was made from, the game's value analysis or None if it failed).
        self.value_analysis: tuple[object, object | None] | None = None

    def analysed_values(self) -> object | None:
        """The game's value analysis, if one was made from the game as it is now."""
        if self.project is None or self.value_analysis is None:
            return None
        key, values = self.value_analysis
        return values if key == value_analysis_key(self.project) else None

    def open(self, root: str | Path) -> None:
        """Load the project at ``root`` and refresh every dependent field."""
        try:
            project = Project.load(root)
        except (FileNotFoundError, ValueError) as error:
            raise InvalidProject(str(error)) from error
        string_files = project.load_string_files()
        source_languages, language_index = source_language_index(
            (
                ((string_file.source_file, unit.id), unit.source)
                for string_file in string_files
                for unit in string_file.units
            ),
            project.config.source_lang,
        )
        self.project = project
        self.string_files = string_files
        self.source_languages = source_languages
        self.source_language_index = language_index
        self.memory = TranslationMemory(string_files)
        self.refresh_glossary()
        self.settings.record_project(project.root)

    def refresh_source_language_index(self) -> None:
        """Rebuild cached source-language membership after catalog replacement."""
        default = self.project.config.source_lang if self.project else ""
        self.source_languages, self.source_language_index = source_language_index(
            (
                ((string_file.source_file, unit.id), unit.source)
                for string_file in self.string_files
                for unit in string_file.units
            ),
            default,
        )

    def reload(self) -> None:
        """Re-run ``open`` against the open project, keeping the edit and conflict in progress."""
        if self.project is not None:
            self.open(self.project.root)

    def refresh_file(self, source_file: str) -> None:
        """Read back one string file a running job has just saved."""
        if self.project is None:
            return
        path = self.project.strings_dir / string_filename(source_file)
        with file_lock(path):
            self.replace_file(load_string_file(path))

    def replace_file(self, fresh: StringFile) -> None:
        """Show ``fresh`` in place of the string file of the same source."""
        self.string_files = [
            fresh if string_file.source_file == fresh.source_file else string_file
            for string_file in self.string_files
        ]

    def find_unit(self, source_file: str, unit_id: str) -> TextUnit | None:
        for string_file in self.string_files:
            if string_file.source_file == source_file:
                return string_file.by_id(unit_id)
        return None

    def begin_edit(
        self, source_file: str, unit_id: str, target: str, baseline: str | None = None
    ) -> None:
        """Track an in-progress edit of the text ``baseline`` so watcher conflicts scope to it."""
        if self.editing is not None and self.editing[:2] == (source_file, unit_id):
            baseline = self.editing[3]
        elif baseline is None:
            unit = self.find_unit(source_file, unit_id)
            baseline = unit.target if unit is not None else ""
        if target == baseline:
            self.editing = None
            return
        self.editing = (source_file, unit_id, target, baseline)

    def settle_edit(self, source_file: str, unit_id: str, saved: str) -> None:
        """Move the edit's baseline to the value just saved."""
        if self.editing is None or self.editing[:2] != (source_file, unit_id):
            return
        draft = self.editing[2]
        self.editing = None if draft == saved else (source_file, unit_id, draft, saved)

    def refresh_glossary(self) -> None:
        """Reload the project glossary mapping after a glossary mutation."""
        if self.project is None:
            return
        self.glossary_mapping = Glossary.load_or_empty(
            self.project.glossary_path
        ).as_mapping()
