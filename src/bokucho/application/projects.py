"""Project creation and engine workflow use cases."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressSink,
)
from bokucho.application.errors import InvalidProject
from bokucho.engines import adapter_class
from bokucho.engines.base import EngineAdapter
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import (
    PROJECT_FILE,
    STRINGS_DIRNAME,
    Project,
    ProjectConfig,
    unpacked_game_dir,
)
from bokucho.sync.atomic import atomic_write_text, file_lock
from bokucho.sync.json_io import load_string_file
from bokucho.translate.glossary import Glossary

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()

PROJECT_TOML_TEMPLATE = """\
# bokucho project configuration
# See: https://github.com/Tong-Luo-Shao/bokucho

[project]
name = "{name}"
source_lang = "{source_lang}"   # language of the original game text (ISO / BCP-47)
target_lang = "{target_lang}"   # language you are translating into

[engine]
type = "{engine}"               # engine adapter to use (plugin seam #1)
game_path = ""                  # path to the game exe/data to extract from

[translator]
provider = "claude"             # claude | openai | local (plugin seam #2)
model = "claude-opus-4-8"
batch_size = 20

[glossary]
path = "glossary.json"

# Faces `bokucho patch-font` draws the proxied characters from, in order: the
# first one that has a character draws it, so a Traditional face can lead and a
# Simplified one cover what it lacks. Pick redistributable fonts (Noto, WenQuanYi)
# -- their glyphs end up inside the game you ship.
# [[font.source]]
# path = "NotoSansTC-VF.ttf"
# variation = "Medium"        # named instance; small cells read better with weight

# Localization pipeline, applied in order by `bokucho transform` (idempotent).
# [[transform]]
# type = "opencc"
# config = "s2twp"            # zh-cn -> zh-tw + vocabulary; s2hk for HK
# custom_dict = "tw-terms.tsv"
#
# [[transform]]
# type = "punctuation"
# profile = "zh-tw"           # curly quotes -> corner brackets

# Optional CAT-platform integration for team review (not needed solo).
# [sync]
# provider = "paratranz"
# project_id = 12345
"""

PROJECT_GITIGNORE = """\
/unpacked/
/out/
/dist/
"""


def _render_project_toml(
    name: str,
    source_lang: str,
    target_lang: str,
    engine: str,
    game_path: str = "",
) -> str:
    config = ProjectConfig(
        name=name, source_lang=source_lang, target_lang=target_lang,
        engine={"type": engine, "game_path": game_path},
    )
    document = tomlkit.parse(
        PROJECT_TOML_TEMPLATE.format(
            name="",
            source_lang="",
            target_lang="",
            engine="",
        )
    )
    document["project"]["name"] = name
    document["project"]["source_lang"] = config.source_lang
    document["project"]["target_lang"] = config.target_lang
    document["engine"]["type"] = engine
    document["engine"]["game_path"] = game_path
    document["engine"]["language"] = config.engine.language
    return tomlkit.dumps(document)


@dataclass(frozen=True, slots=True)
class CreateProjectRequest:
    destination: Path
    name: str
    source_lang: str
    target_lang: str
    engine: str
    force: bool = False
    game_path: str = ""


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    root: Path
    name: str
    source_lang: str
    target_lang: str
    engine: str
    total_units: int
    status_counts: dict[Status, int]
    image_units: int


@dataclass(frozen=True, slots=True)
class ExtractResult:
    units: int
    files: int
    strings_dir: Path
    added: int = 0
    kept: int = 0
    gone: int = 0


@dataclass(frozen=True, slots=True)
class InjectResult:
    translated: int
    files: int
    total: int
    out_dir: Path


def create_project(request: CreateProjectRequest) -> Project:
    """Create or explicitly replace a Bokucho project scaffold."""
    root = request.destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_file = root / PROJECT_FILE
    if project_file.exists() and not request.force:
        raise InvalidProject(
            f"{PROJECT_FILE} already exists in {root}. Use force to overwrite."
        )

    name = request.name or root.name or "bokucho-project"
    atomic_write_text(
        project_file,
        _render_project_toml(
            name,
            request.source_lang,
            request.target_lang,
            request.engine,
            request.game_path,
        ),
    )
    (root / STRINGS_DIRNAME).mkdir(exist_ok=True)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        atomic_write_text(gitignore, PROJECT_GITIGNORE)
    project = Project.load(root)
    if not project.glossary_path.exists():
        Glossary().save(project.glossary_path)
    return project


def load_project_summary(root: str | Path) -> ProjectSummary:
    """Load dashboard counts without exposing presentation concerns."""
    try:
        project = Project.load(root)
    except (FileNotFoundError, ValueError) as exc:
        raise InvalidProject(str(exc)) from exc
    return summarize_string_files(project, project.load_string_files())


def summarize_string_files(
    project: Project, string_files: list[StringFile]
) -> ProjectSummary:
    """Build dashboard counts from already-loaded string files."""
    units = [unit for string_file in string_files for unit in string_file.units]
    counts = Counter(unit.status for unit in units)
    return ProjectSummary(
        root=project.root.resolve(),
        name=project.config.name,
        source_lang=project.config.source_lang,
        target_lang=project.config.target_lang,
        engine=project.config.engine.type,
        total_units=len(units),
        status_counts={status: counts[status] for status in Status},
        image_units=sum(unit.kind == "image" for unit in units),
    )


def resolve_game_path(project: Project) -> Path:
    """Resolve and validate the engine input path configured by a project."""
    raw = project.config.engine.game_path
    if not raw:
        raise InvalidProject(
            "Set [engine] game_path in project.toml before extracting."
        )
    path = Path(raw)
    if not path.is_absolute():
        path = project.root / path
    if not path.exists():
        raise InvalidProject(f"Game path does not exist: {path}")
    return path


def resolve_game_root(project: Project) -> Path:
    """The configured game as a directory of files."""
    path = resolve_game_path(project)
    adapter = adapter_class(project.config.engine.type)
    root = adapter.game_root(path)
    if root is not None:
        return root
    archive = adapter.packed_archive(path)
    if path.is_dir() and archive == path:
        return path
    unpacked = unpacked_game_dir(project.root, archive)
    if unpacked.is_dir():
        return unpacked
    raise InvalidProject(
        f"{archive.name} is a packed archive that has not been unpacked yet. "
        "Extract the text first — extraction unpacks the game into the project "
        "(the Extract button on the Strings screen, or `bokucho extract`)."
    )


def string_filename(source_file: str) -> str:
    """Flatten an engine-relative source path into ``strings/*.json``."""
    return source_file.replace("/", "__") + ".json"


def injectable_units(project: Project) -> list[TextUnit]:
    """Every unit whose write-back anchor still points at something."""
    return [
        unit
        for string_file in project.load_string_files()
        for unit in string_file.units
        if not unit.missing
    ]


def _settle_if_nothing_to_translate(unit: TextUnit) -> TextUnit:
    """A string of only whitespace, already answered."""
    if unit.source.strip():
        return unit
    return unit.model_copy(update={"target": unit.source, "status": Status.IGNORED})


def _merge_unit(existing: TextUnit, extracted: TextUnit) -> TextUnit:
    """``extracted``, carrying whatever was decided about ``existing``."""
    #: A script unit's address is a line and column, so new text there is another literal.
    moved = "script" in (existing.kind, extracted.kind) and existing.source != extracted.source
    if moved:
        from bokucho.application.text import _keep_overwritten

        _keep_overwritten(existing)
    settled = moved or (
        extracted.status is Status.IGNORED
        and not existing.target.strip()
        and not existing.history
    )
    merged = extracted.model_copy(
        update={
            "target": extracted.target if settled else existing.target,
            "status": extracted.status if settled else existing.status,
            "history": existing.history,
            "context": existing.context,
            "kind": existing.kind,
        }
    )
    merged.engine_ref = {**existing.engine_ref, **extracted.engine_ref}
    if not moved and existing.source != extracted.source and existing.status is Status.REVIEWED:
        merged.status = Status.MT
    return merged


def _stitched(unit: TextUnit, previous: StringFile) -> TextUnit:
    """A unit made of pieces the project held separately, given their work."""
    parts = unit.engine_ref.get("parts")
    if not parts:
        return unit
    pieces = {
        part["block"]: previous.by_id(part["block"]) for part in parts if "block" in part
    }
    if any(piece is None for piece in pieces.values()):
        return unit
    joined: list[str] = []
    for part in parts:
        if "var" in part:
            joined.append(f"<VAR {part['var']}>")
        elif "text" in part:
            joined.append(part["text"])
        else:
            piece = pieces[part["block"]]
            if piece.target:
                joined.append(piece.target)
            elif not piece.source.strip():
                joined.append(piece.source)
            else:
                return unit
    return unit.model_copy(update={"target": "".join(joined), "status": Status.MT})


def extract_text(
    project: Project,
    adapter: EngineAdapter,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
) -> ExtractResult:
    """Extract engine text and persist one neutral file per source file."""
    game_path = resolve_game_path(project)
    token.raise_if_cancelled()
    units = [
        _settle_if_nothing_to_translate(unit)
        for unit in adapter.extract(game_path, sink, token)
    ]
    groups: dict[str, list[TextUnit]] = defaultdict(list)
    for unit in units:
        token.raise_if_cancelled()
        source = unit.engine_ref.get("lsb") or unit.engine_ref.get("file") or "_unknown"
        groups[str(source)].append(unit)
    for string_file in project.load_string_files():
        groups.setdefault(string_file.source_file, [])
    added = kept = gone = 0
    for index, (source_file, grouped) in enumerate(sorted(groups.items()), start=1):
        token.raise_if_cancelled()
        path = project.strings_dir / string_filename(source_file)
        with file_lock(path):
            previous = load_string_file(path) if path.is_file() else None
            merged, counts = _merged_extract(previous, grouped)
            project.save_string_file(
                StringFile(source_file=source_file, units=merged),
                string_filename(source_file),
            )
        added, kept, gone = added + counts[0], kept + counts[1], gone + counts[2]
        sink.publish(
            ProgressEvent(
                "extract",
                index,
                len(groups),
                "extract.file.saved",
                detail=source_file,
            )
        )
    return ExtractResult(
        len(units), len(groups), project.strings_dir, added, kept, gone
    )


def _merged_extract(
    previous: StringFile | None, grouped: list[TextUnit]
) -> tuple[list[TextUnit], tuple[int, int, int]]:
    """The fresh units over the file on disk, and how many were added, kept and gone."""
    if previous is None:
        return grouped, (len(grouped), 0, 0)
    merged: list[TextUnit] = []
    added = kept = 0
    seen = set()
    for unit in grouped:
        before = previous.by_id(unit.id)
        if before is None:
            merged.append(_stitched(unit, previous))
            added += 1
        else:
            merged.append(_merge_unit(before, unit))
            kept += 1
        seen.add(unit.id)
    gone = 0
    for unit in previous.units:
        if unit.id not in seen:
            unit.missing = True
            merged.append(unit)
            gone += 1
    return merged, (added, kept, gone)


def inject_translations(
    project: Project,
    adapter: EngineAdapter,
    out_dir: Path,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
) -> InjectResult:
    """Inject project text into a separate game overlay directory."""
    game_path = resolve_game_path(project)
    units = injectable_units(project)
    if not units:
        raise InvalidProject("No text units found. Run `bokucho extract` first.")
    token.raise_if_cancelled()
    adapter.inject(units, game_path, out_dir, sink, token)
    token.raise_if_cancelled()
    files = len(
        {unit.engine_ref.get("lsb") or unit.engine_ref.get("file") for unit in units}
    )
    translated = sum(bool(unit.target) for unit in units)
    sink.publish(ProgressEvent("inject", 1, 1, "inject.completed", detail=str(out_dir)))
    return InjectResult(translated, files, len(units), out_dir)
