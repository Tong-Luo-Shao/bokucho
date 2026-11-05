"""Structured project quality assurance workflows."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from functools import cache, lru_cache
from pathlib import Path

from bokucho.application.contracts import CancellationToken, NeverCancelled
from bokucho.application.errors import InvalidProject
from bokucho.application.projects import string_filename
from bokucho.brackets import bracket_findings
from bokucho.findings import Finding
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.qa import Issue, check_unit, encodable, format_warnings, traditional_codec
from bokucho.sync.atomic import atomic_write_text
from bokucho.sync.json_io import held_string_file, string_paths


@dataclass(frozen=True, slots=True)
class QaReport:
    status_counts: dict[Status, int]
    issues: tuple[Issue, ...]

    @property
    def total(self) -> int:
        return sum(self.status_counts.values())

    @property
    def blocking_issues(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity != "warning")

    @property
    def passed(self) -> bool:
        return self.total > 0 and not self.blocking_issues


_SIMPLIFIED_IN_BIG5 = frozenset("么后种体")

# ponytail: the places one game puts things in; add the next one a game leaves.
_INSIDE = re.compile(
    "(?<=[這那哪心家嘴手夜屋子眼懷夢腦肚城窩洞間房袋箱籠牢獄水井鍋碗杯瓶盒包車船網床])里"
    "|里(?=[面頭邊])"
)

# ponytail: one game's list; extend it with the next form a game leaves in.
_JAPANESE_FORMS = {"姫": "姬", "呪": "咒", "氷": "冰", "鋭": "銳", "躙": "躪"}


@cache
def hans_converter(target_lang: str) -> Callable[[str], str] | None:
    """A Traditional target as it reads once its Simplified residue is converted."""
    codec = traditional_codec(target_lang)
    if codec is None:
        return None
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    regional = OpenCC("s2hk" if codec == "big5hkscs" else "s2tw").convert
    inside = "裏" if codec == "big5hkscs" else "裡"
    japanese = OpenCC("jp2t").convert

    def aligned(convert: Callable[[str], str], text: str) -> str:
        converted = convert(text)
        if len(converted) == len(text):
            return converted
        return "".join(new if len(new := convert(old)) == 1 else old for old in text)

    @lru_cache(maxsize=4096)
    def convert(text: str) -> str:
        out = list(text)
        for index, (old, chinese, japan) in enumerate(
            zip(text, aligned(regional, text), aligned(japanese, text), strict=True)
        ):
            if old not in _SIMPLIFIED_IN_BIG5 and encodable(old, codec):
                continue
            new = chinese if chinese != old else japan
            if new == old:
                new = _JAPANESE_FORMS.get(old, old)
            if encodable(new, codec):
                out[index] = new
        return _INSIDE.sub(inside, "".join(out))

    return convert


def _translated(unit: TextUnit) -> bool:
    """Whether the game gets ``unit``'s target: write-back ships any non-empty one, status aside."""
    return bool(unit.target)


def _shipped(unit: TextUnit) -> str:
    """The text the game ends up with for ``unit``."""
    return unit.target if _translated(unit) else unit.source


def _script_findings(string_files: list[StringFile]) -> Iterator[Issue]:
    """Warnings for script literals the game compares or loads as files, shipped changed."""
    by_source: defaultdict[str, list[TextUnit]] = defaultdict(list)
    for string_file in string_files:
        for unit in string_file.units:
            if not unit.missing:
                by_source[unit.source].append(unit)
    for string_file in string_files:
        for unit in string_file.units:
            if unit.missing:
                continue
            here = _shipped(unit)
            if unit.engine_ref.get("key"):
                other = next((o for o in by_source[unit.source] if _shipped(o) != here), None)
                if other is not None:
                    params = {
                        "source": unit.source,
                        "here": here,
                        "elsewhere": _shipped(other),
                        "where": other.id,
                    }
                    yield Issue.of(
                        string_file.source_file,
                        unit.id,
                        Finding("script", "script_key", params),
                        severity="warning",
                    )
            if (file := unit.engine_ref.get("asset")) and _translated(unit):
                yield Issue.of(
                    string_file.source_file,
                    unit.id,
                    Finding("script", "script_asset", {"source": unit.source, "file": file}),
                    severity="warning",
                )


def qa_report(project: Project, string_files: Iterable[StringFile]) -> QaReport:
    """Evaluate the units handed in, reading nothing from disk."""
    string_files = list(string_files)
    hans_convert = hans_converter(project.config.target_lang)
    brackets = _compares_brackets(project)
    reject_echo = project.config.source_lang != project.config.target_lang
    counts: Counter[Status] = Counter()
    issues: list[Issue] = []
    for string_file in string_files:
        for unit in string_file.units:
            counts[unit.status] += 1
            if not unit.target:
                continue
            issues.extend(
                Issue.of(string_file.source_file, unit.id, finding)
                for finding in check_unit(
                    unit,
                    hans_convert,
                    reject_echo,
                    project.config.engine.type,
                    project.config.target_lang,
                    project.config.source_lang,
                )
            )
            issues.extend(
                Issue.of(string_file.source_file, unit.id, finding, severity="warning")
                for finding in format_warnings(unit.source, unit.target)
            )
        if brackets:
            issues.extend(
                Issue.of(string_file.source_file, unit_id, finding, severity="warning")
                for found in bracket_findings(string_file.units, project.config.engine.type)
                for unit_id, finding in found.findings
            )
    issues.extend(_script_findings(string_files))
    if not counts:
        raise InvalidProject("No text units found. Run `bokucho extract` first.")
    return QaReport(
        status_counts={status: counts[status] for status in Status},
        issues=tuple(issues),
    )


def run_qa(project: Project, json_out: Path | None = None) -> QaReport:
    """Evaluate the project on disk, optionally writing an atomic JSON report."""
    report = qa_report(project, project.load_string_files())
    if json_out is not None:
        atomic_write_text(
            json_out,
            json.dumps(
                [asdict(issue) for issue in report.issues],
                ensure_ascii=False,
                indent=1,
            ),
        )
    return report


_BRACKET_LANGUAGES = frozenset({"ja", "zh"})


def _compares_brackets(project: Project) -> bool:
    config = project.config
    return {config.source_lang.split("-")[0], config.target_lang.split("-")[0]} <= (
        _BRACKET_LANGUAGES
    )


_NEVER_CANCELLED = NeverCancelled()


@dataclass(frozen=True, slots=True)
class BracketFixResult:
    fixed: int


def _bracket_fixes(project: Project, string_file: StringFile) -> tuple[int, dict[str, str]]:
    """How many messages a rule puts right, and unit id -> target, in machine drafts only."""
    if not _compares_brackets(project):
        return 0, {}
    status = {unit.id: unit.status for unit in string_file.units}
    count, fixes = 0, {}
    for finding in bracket_findings(string_file.units, project.config.engine.type):
        if finding.fixes and all(status[unit_id] is Status.MT for unit_id in finding.fixes):
            count += 1
            fixes.update(finding.fixes)
    return count, fixes


def bracket_fix_report(project: Project, string_files: Iterable[StringFile]) -> BracketFixResult:
    """What :func:`fix_brackets` would put right in the files handed in, changing none."""
    return BracketFixResult(sum(_bracket_fixes(project, f)[0] for f in string_files))


def fix_brackets(
    project: Project, token: CancellationToken = _NEVER_CANCELLED
) -> BracketFixResult:
    """Put back the brackets a mechanical rule can, in machine drafts."""
    fixed = 0
    for path in string_paths(project.strings_dir):
        token.raise_if_cancelled()
        with held_string_file(path) as string_file:
            count, fixes = _bracket_fixes(project, string_file)
            if not fixes:
                continue
            for unit in string_file.units:
                unit.target = fixes.get(unit.id, unit.target)
            project.save_string_file(string_file, path.name)
            fixed += count
    return BracketFixResult(fixed)


@dataclass(frozen=True, slots=True)
class ValueFixResult:
    #: Values given their parts' translations so the game's checks on them hold.
    fixed: int


def fix_values(
    project: Project, assemble: Callable[[list[TextUnit]], dict[str, str]]
) -> ValueFixResult:
    """Write what ``assemble`` returns for the project's units -- unit id -> target."""
    string_files = project.load_string_files()
    targets = assemble(
        [unit for string_file in string_files for unit in string_file.units if not unit.missing]
    )
    before = {
        (string_file.source_file, unit.id): (unit.target, unit.status)
        for string_file in string_files
        for unit in string_file.units
        if unit.id in targets
    }
    fixed = 0
    for listed in string_files:
        if not any(unit.id in targets for unit in listed.units):
            continue
        filename = string_filename(listed.source_file)
        with held_string_file(project.strings_dir / filename) as string_file:
            units = [
                unit
                for unit in string_file.units
                if unit.id in targets
                and before.get((string_file.source_file, unit.id))
                == (unit.target, unit.status)
            ]
            for unit in units:
                unit.target = targets[unit.id]
            if units:
                project.save_string_file(string_file, filename)
        fixed += len(units)
    return ValueFixResult(fixed)
