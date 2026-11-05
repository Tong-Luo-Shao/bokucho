"""Translation, transformation, import, and review use cases."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressSink,
)
from bokucho.application.errors import (
    ProviderFailure,
    ProviderMiscounted,
    ProviderRefused,
    ValidationFailure,
)
from bokucho.application.projects import string_filename
from bokucho.application.quality import hans_converter
from bokucho.application.translation_limits import (
    BATCH_ATTEMPTS,
    CONSECUTIVE_BATCH_FAILURES,
    DIAGNOSTIC_LIMIT,
    HANDED_BACK_RETRIES,
    PromptTokenCounter,
    RequestLimiter,
    TokenBatch,
    TranslationLane,
    bounded_diagnostic,
    request_token_budget,
    sanitize_diagnostic,
    send_with_retries,
    take_token_batch,
)
from bokucho.layout import TextLayout, apply_layout, layout_content, reflow, split_layout
from bokucho.model import HISTORY_CAP, HistoryEntry, Status, StringFile, TextUnit
from bokucho.project import Project, PromptConfig
from bokucho.qa import (
    around_markup,
    dropped_lines,
    echoes_source,
    foreign_text,
    format_issues,
    format_warnings,
    has_word,
    kana_residue,
    markup_patterns,
    problem_marks,
    source_layout,
    without_markup,
)
from bokucho.sync.atomic import file_lock
from bokucho.sync.json_io import held_string_file, load_string_file, string_paths
from bokucho.transform.base import Transform
from bokucho.translate.glossary import (
    Glossary,
    load_global_glossary,
    merged_mapping,
)
from bokucho.translate.prompts import find_preset
from bokucho.translators.base import (
    TokenUsage,
    TranslationContext,
    Translator,
    build_prompt,
    judging_replies,
    log_translation_setup,
    replies_shown_whole,
)

_NARROWED = (ProviderRefused, ProviderMiscounted)

_SMALL_KANA = str.maketrans("", "", "っッぁぃぅぇぉァィゥェォ")

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()
_log = logging.getLogger(__name__)


def effective_glossary(project: Project) -> dict[str, str]:
    """The project glossary merged over the user-level global one."""
    project_glossary = Glossary.load_or_empty(project.glossary_path)
    try:
        global_glossary = load_global_glossary()
    except Exception:  # noqa: BLE001 - any parse error just disables the global side
        _log.warning("Ignoring corrupt global glossary; using project terms only.")
        global_glossary = Glossary()
    return merged_mapping(project_glossary, global_glossary)


def effective_prompts(project: Project) -> PromptConfig:
    """The wording a run will send."""
    preset_id = project.config.prompts.preset_id
    if not preset_id:
        return project.config.prompts
    preset = find_preset(preset_id)
    if preset is None:
        _log.warning("Prompt preset %r is missing; using the project's own.", preset_id)
        return project.config.prompts
    return preset.as_prompt_config()


def _quoted(answer: str) -> str:
    """``answer`` as one readable line, cut where it stops being worth reading."""
    answer = sanitize_diagnostic(answer)
    quoted = repr(answer)
    if len(quoted) <= DIAGNOSTIC_LIMIT:
        return quoted
    end = min(len(answer), DIAGNOSTIC_LIMIT - 5)
    while len(quoted := repr(f"{answer[:end]}...")) > DIAGNOSTIC_LIMIT:
        end -= 1
    return quoted


def _region_unit(unit: TextUnit, source_lang: str = "", engine: str = "") -> TextUnit:
    """``unit`` with its layout stripped, for sending to a provider."""
    layout, region = source_layout(unit.source, source_lang, engine)
    return unit.model_copy(update={
        "source": region,
        "target": layout_content(layout, unit.target) or "",
    })


@dataclass(frozen=True, slots=True)
class TranslationResult:
    translated: int
    failed: int
    processed: int
    total: int
    batches: int
    lanes: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    total_tokens: int
    estimated_input_tokens: int
    token_usage_reported: bool
    elapsed_seconds: float


@dataclass(slots=True)
class _TranslationStats:
    total: int
    lanes: int
    started_at: float
    translated: int = 0
    failed: int = 0
    processed: int = 0
    batches: int = 0
    estimated_input_tokens: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)

    def snapshot(self, completed: int, now: float) -> dict[str, object]:
        elapsed = max(now - self.started_at, 0.0)
        if completed >= self.total:
            eta: float | None = 0.0
        elif completed > 0:
            eta = elapsed * (self.total - completed) / completed
        else:
            eta = None
        return {
            "processed": completed,
            "total": self.total,
            "translated": self.translated,
            "failed": self.failed,
            "batches": self.batches,
            "lanes": self.lanes,
            "requests": self.usage.requests,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cached_input_tokens": self.usage.cached_input_tokens,
            "total_tokens": self.usage.total_tokens,
            "estimated_input_tokens": self.estimated_input_tokens,
            "token_usage_reported": self.usage.requests > 0,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
        }

    def result(self, now: float) -> TranslationResult:
        elapsed = max(now - self.started_at, 0.0)
        return TranslationResult(
            translated=self.translated,
            failed=self.failed,
            processed=self.processed,
            total=self.total,
            batches=self.batches,
            lanes=self.lanes,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
            cached_input_tokens=self.usage.cached_input_tokens,
            total_tokens=self.usage.total_tokens,
            estimated_input_tokens=self.estimated_input_tokens,
            token_usage_reported=self.usage.requests > 0,
            elapsed_seconds=elapsed,
        )


class _ReplyProgress:
    """How much of the replies in flight has arrived, shown while it arrives."""

    INTERVAL = 0.25

    def __init__(self, publish: Callable[[int], None], clock: Callable[[], float]) -> None:
        self._publish = publish
        self._clock = clock
        self._lock = threading.Lock()
        self._received: dict[int, int] = {}
        self._shown_at: float | None = None

    def received(self, chars: int) -> None:
        with self._lock:
            self._received[threading.get_ident()] = chars
            if not chars:
                return
            now = self._clock()
            if self._shown_at is not None and now - self._shown_at < self.INTERVAL:
                return
            self._shown_at = now
            self._publish(sum(self._received.values()))

    def clear(self) -> None:
        """The batches in flight have all come back."""
        with self._lock:
            self._received.clear()


def _listen_to_replies(
    lanes: Sequence[TranslationLane], listener: Callable[[int], None] | None
) -> None:
    for lane in lanes:
        set_listener = getattr(lane.translator, "set_stream_listener", None)
        if callable(set_listener):
            set_listener(listener)


@dataclass(frozen=True, slots=True)
class _BatchTask:
    """One batch, ready to send: what goes out and what puts it back."""

    units: tuple[TextUnit, ...]
    regions: tuple[TextUnit, ...]
    layouts: tuple[TextLayout, ...]
    context: TranslationContext
    input_tokens: int
    source_files: tuple[str, ...]


def _prepare_batch(
    owners: Mapping[int, StringFile],
    token_batch: TokenBatch,
    context: TranslationContext,
    context_units: int,
) -> _BatchTask:
    batch = list(token_batch.units)
    first_file = owners[id(batch[0])]
    start = first_file.units.index(batch[0])
    preceding = tuple(
        _region_unit(unit, context.source_lang, context.engine)
        for unit in first_file.units[max(0, start - context_units) : start]
        if source_layout(unit.source, context.source_lang, context.engine)[1]
    )
    split = [source_layout(unit.source, context.source_lang, context.engine) for unit in batch]
    return _BatchTask(
        units=tuple(batch),
        regions=tuple(_region_unit(unit, context.source_lang, context.engine) for unit in batch),
        layouts=tuple(layout for layout, _ in split),
        context=context.model_copy(update={"preceding": preceding}),
        input_tokens=token_batch.input_tokens,
        source_files=tuple(
            dict.fromkeys(owners[id(unit)].source_file for unit in batch)
        ),
    )


@dataclass(frozen=True, slots=True)
class TransformResult:
    changed: int
    transforms: int


@dataclass(frozen=True, slots=True)
class ImportResult:
    imported: int
    unmatched: int


@dataclass(frozen=True, slots=True)
class RecheckResult:
    settled: int
    sent_back: int


@dataclass(frozen=True, slots=True)
class ReviewResult:
    marked: int
    missing: tuple[str, ...]


def _find_unit(
    project: Project, source_file: str, unit_id: str
) -> tuple[StringFile, TextUnit]:
    path = project.strings_dir / string_filename(source_file)
    if path.is_file():
        string_file = load_string_file(path)
        if string_file.source_file == source_file:
            unit = string_file.by_id(unit_id)
            if unit is not None:
                return string_file, unit
    raise ValidationFailure(f"Text unit not found: {source_file} {unit_id}")


def list_units(
    project: Project, status: Status | None = None
) -> list[tuple[str, TextUnit]]:
    """Return project units with their owning source-file identifiers."""
    return [
        (string_file.source_file, unit)
        for string_file in project.load_string_files()
        for unit in string_file.units
        if status is None or unit.status is status
    ]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def record_transition(
    unit: TextUnit,
    new_status: Status,
    now: Callable[[], str] | None = None,
) -> None:
    """Append a history entry when ``unit`` actually changes status."""
    if unit.status is new_status:
        return
    unit.history.append(
        HistoryEntry(target=unit.target, status=new_status, at=(now or _utc_now)())
    )
    del unit.history[:-HISTORY_CAP]


def _keep_overwritten(unit: TextUnit) -> None:
    """Put ``unit``'s current target in its history unless the history already ends with it."""
    if not unit.target or (unit.history and unit.history[-1].target == unit.target):
        return
    unit.history.append(
        HistoryEntry(target=unit.target, status=unit.status, at=_utc_now())
    )
    del unit.history[:-HISTORY_CAP]


def save_target(
    project: Project, source_file: str, unit_id: str, target: str
) -> TextUnit:
    """Atomically save an editor draft without changing its workflow status."""
    with file_lock(project.strings_dir / string_filename(source_file)):
        string_file, unit = _find_unit(project, source_file, unit_id)
        unit.target = target
        project.save_string_file(string_file, string_filename(source_file))
    return unit.model_copy(deep=True)


def mark_reviewed(
    project: Project,
    source_file: str,
    unit_id: str,
    enforce_format: bool = True,
) -> TextUnit:
    """Mark a translated unit reviewed after optional format validation."""
    with file_lock(project.strings_dir / string_filename(source_file)):
        string_file, unit = _find_unit(project, source_file, unit_id)
        if not unit.target:
            raise ValidationFailure(f"Text unit has no target: {unit_id}")
        if enforce_format:
            issues = format_issues(
                unit.source,
                unit.target,
                engine=project.config.engine.type,
                target_lang=project.config.target_lang,
                script=unit.kind == "script",
            )
            if issues:
                raise ValidationFailure("; ".join(issues))
        record_transition(unit, Status.REVIEWED)
        unit.status = Status.REVIEWED
        project.save_string_file(string_file, string_filename(source_file))
    return unit.model_copy(deep=True)


SETTABLE_STATUSES: frozenset[Status] = frozenset({Status.UNTRANSLATED, Status.MT, Status.IGNORED})


def set_status(
    project: Project,
    source_file: str,
    unit_id: str,
    status: Status,
) -> TextUnit:
    """Hand a unit back to an earlier stage of the flow, or set it aside."""
    if status not in SETTABLE_STATUSES:
        raise ValidationFailure(f"Status cannot be set directly: {status.value}")
    with file_lock(project.strings_dir / string_filename(source_file)):
        string_file, unit = _find_unit(project, source_file, unit_id)
        if unit.status is not status:
            # An ignored line ships as the original: every write-back and QA
            # check skips an empty target, so a draft left on it would still go out.
            if status is Status.IGNORED:
                _keep_overwritten(unit)
                unit.target = ""
            elif unit.status is Status.IGNORED and not unit.target:
                unit.target = next(
                    (entry.target for entry in reversed(unit.history) if entry.target), ""
                )
            record_transition(unit, status)
            unit.status = status
            project.save_string_file(string_file, string_filename(source_file))
    return unit.model_copy(deep=True)


def _merge_drafts(
    project: Project,
    source_file: str,
    drafts: Sequence[TextUnit],
    before: Mapping[int, tuple[str, Status]],
) -> int:
    """Save a run's drafts into the file as it is now; return how many lost to an edit."""
    filename = string_filename(source_file)
    with file_lock(project.strings_dir / filename):
        current = load_string_file(project.strings_dir / filename)
        on_disk = {unit.id: unit for unit in current.units}
        edited = 0
        for draft in drafts:
            unit = on_disk.get(draft.id)
            if unit is None or (unit.target, unit.status) != before[id(draft)]:
                _log.info(
                    "%s %s was edited while it was translated; the edit stays",
                    source_file,
                    draft.id,
                )
                edited += 1
                continue
            unit.target, unit.status, unit.history = draft.target, draft.status, draft.history
        project.save_string_file(current, filename)
    return edited


def mark_units_reviewed(
    project: Project,
    unit_ids: Sequence[str],
    enforce_format: bool = True,
) -> ReviewResult:
    """Mark multiple unit ids in one pass, saving each changed file once."""
    remaining = set(unit_ids)
    marked = 0
    for path in string_paths(project.strings_dir):
        with held_string_file(path) as string_file:
            changed = False
            for unit in string_file.units:
                if unit.id not in remaining or not unit.target:
                    continue
                if enforce_format:
                    issues = format_issues(
                        unit.source,
                        unit.target,
                        engine=project.config.engine.type,
                        target_lang=project.config.target_lang,
                        script=unit.kind == "script",
                    )
                    if issues:
                        continue
                remaining.discard(unit.id)
                record_transition(unit, Status.REVIEWED)
                unit.status = Status.REVIEWED
                changed = True
                marked += 1
            if changed:
                project.save_string_file(string_file, path.name)
    return ReviewResult(marked, tuple(sorted(remaining)))


def settle_answer(answer: str, target_lang: str, engine: str = "") -> str:
    """``answer`` as a translation into ``target_lang`` is saved."""
    drop_small_kana = target_lang.split("-")[0].lower() == "zh"
    traditional = hans_converter(target_lang)

    def settle(text: str) -> str:
        if drop_small_kana:
            text = text.translate(_SMALL_KANA)
        if traditional is not None:
            text = traditional(text)
        return text

    return around_markup(answer, markup_patterns(engine), settle)


def _translate_batch(
    lanes: Sequence[TranslationLane],
    lane_index: int,
    units: list[TextUnit],
    context: TranslationContext,
    glossary: Mapping[str, str],
    *,
    input_tokens: int,
    token: CancellationToken,
    sleep: Callable[[float], None],
) -> list[str | ProviderFailure]:
    """Send one batch down a lane, halving it when it comes back unusable."""

    def again(
        part: list[TextUnit], part_context: TranslationContext, part_tokens: int
    ) -> list[str | ProviderFailure]:
        return _translate_batch(
            lanes,
            lane_index,
            part,
            part_context,
            glossary,
            input_tokens=part_tokens,
            token=token,
            sleep=sleep,
        )

    try:
        answers = send_with_retries(
            lanes,
            lane_index,
            lambda lane: lane.translator.translate(units, context, glossary),
            input_tokens=input_tokens,
            token=token,
            sleep=sleep,
        )
    except _NARROWED as exc:
        refused = isinstance(exc, ProviderRefused)
        if refused and context.preceding:
            _log.info(
                "%d line(s) refused with the lines before them; asking again without",
                len(units),
            )
            return again(units, context.model_copy(update={"preceding": ()}), input_tokens)
        if len(units) < 2:
            raise
        _log.info(
            "%d line(s) %s; asking again in halves",
            len(units),
            "refused together" if refused else "came back miscounted",
        )
        return _in_halves(units, input_tokens, lambda half, tokens: again(half, context, tokens))
    return [settle_answer(answer, context.target_lang, context.engine) for answer in answers]


def _in_halves(
    units: list[TextUnit],
    input_tokens: int,
    send: Callable[[list[TextUnit], int], list[str | ProviderFailure]],
) -> list[str | ProviderFailure]:
    """Each half sent on its own; a half that fails marks its own lines, unless both fail."""
    middle = len(units) // 2
    answers: list[str | ProviderFailure] = []
    failed = False
    for half in (units[:middle], units[middle:]):
        try:
            answers += send(half, input_tokens * len(half) // len(units))
        except ProviderFailure as failure:
            if failed:
                raise
            failed = True
            answers += [failure] * len(half)
    return answers


def _skipped_lines(
    units: Sequence[TextUnit],
    answers: Sequence[str | ProviderFailure],
    target_lang: str,
    engine: str,
) -> list[int]:
    """Which lines came back the way they went out -- the shape of a line not done."""
    skipped = []
    for index, (unit, answer) in enumerate(zip(units, answers, strict=True)):
        if isinstance(answer, ProviderFailure):
            continue
        source = without_markup(unit.source, engine)
        answer = without_markup(answer, engine)
        if (
            (kana_residue(source) and echoes_source(source, answer))
            or foreign_text(source, answer, target_lang)
            or dropped_lines(source, answer)
        ):
            skipped.append(index)
    return skipped


class _PausedTimeExcluded:
    """``token`` with the time a paused run waits in it taken off the clock."""

    def __init__(
        self,
        token: CancellationToken,
        stats: _TranslationStats,
        clock: Callable[[], float],
    ) -> None:
        self._token = token
        self._stats = stats
        self._clock = clock
        self._lock = threading.Lock()
        self._waiting = 0
        self._since = 0.0

    def add_cancel_callback(self, callback: Callable[[], None]) -> None:
        self._token.add_cancel_callback(callback)

    def raise_if_cancelled(self) -> None:
        with self._lock:
            if self._waiting == 0:
                self._since = self._clock()
            self._waiting += 1
        try:
            self._token.raise_if_cancelled()
        finally:
            with self._lock:
                self._waiting -= 1
                if self._waiting == 0:
                    self._stats.started_at += self._clock() - self._since


class _TranslationRun:
    """One translation run: what it asks, what came back, and what it saves."""

    def __init__(
        self,
        project: Project,
        lanes: Sequence[TranslationLane],
        *,
        token_counter: PromptTokenCounter | None,
        sink: ProgressSink,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        redo: bool,
    ) -> None:
        self.project = project
        self.lanes = lanes
        self.token_counter = token_counter
        self.sink = sink
        self.sleep = sleep
        self.clock = clock
        self.redo = redo
        self.glossary = effective_glossary(project)
        self.prompts = effective_prompts(project)
        self.target_lang = project.config.target_lang
        self.reject_echo = project.config.source_lang != self.target_lang
        self.engine = project.config.engine.type
        self.context = TranslationContext(
            source_lang=project.config.source_lang,
            target_lang=self.target_lang,
            system_prompt=self.prompts.system,
            user_prompt=self.prompts.user,
            engine=self.engine,
        )
        self.interface = project.config.translator.selected_interface()
        self.slots = len(lanes) * self.interface.concurrency
        self.batch_size = self.interface.batch_size
        self._require_what_the_interface_needs()
        self.budget = request_token_budget(
            self.interface.max_request_tokens, self.interface.tpm
        )
        self.known: dict[str, str] = {}
        self.unanswered: dict[str, ProviderFailure] = {}
        self.handed_back: dict[str, str] = {}
        self.times_handed_back: Counter[str] = Counter()
        self.failed_whole: set[str] = set()
        self.unanswered_by_failure: Counter[str] = Counter()
        self.processed_units: set[int] = set()
        self.saved_files: set[str] = set()
        self.fatal_error: ProviderFailure | None = None
        self.never_sent = 0
        self.consecutive_failures = 0
        self.shown_processed = 0
        self.setup_logged = False

    def _require_what_the_interface_needs(self) -> None:
        interface = self.interface
        if self.batch_size < 1 and interface.max_request_tokens < 1:
            raise ValidationFailure(
                "A request needs a size: set translator.batch_size or max_request_tokens."
            )
        if (interface.tpm or interface.max_request_tokens) and self.token_counter is None:
            raise ValidationFailure(
                "A tiktoken counter is required when a token budget is configured."
            )
        if (interface.rpm or interface.tpm or interface.rpd) and any(
            lane.limiter is None for lane in self.lanes
        ):
            raise ValidationFailure(
                "A request limiter is required when provider quotas are configured."
            )

    def _log_setup(self) -> None:
        interface = self.interface
        _log.info(
            "Translating with %s/%s (%d token(s) per request, %s unit cap, "
            "%d lane(s), up to %d request(s) in flight)",
            interface.provider,
            interface.model,
            self.budget,
            self.batch_size or "no",
            len(self.lanes),
            self.slots,
        )
        if interface.context_units and "{context_section}" not in self.prompts.user:
            _log.warning(
                "Context is configured (%d line(s)) but the user prompt has no "
                "{context_section} placeholder, so none is sent.",
                interface.context_units,
            )

    def _region(self, unit: TextUnit) -> str:
        return source_layout(unit.source, self.context.source_lang, self.engine)[1]

    def _pending(self, unit: TextUnit) -> bool:
        return (self.redo and unit.status is Status.MT) or unit.needs_translation

    def _wanted(self, unit: TextUnit) -> bool:
        return self._pending(unit) and bool(self._region(unit))

    def _count(self, string_files: list[StringFile], limit: int | None) -> int:
        written = [
            unit
            for string_file in string_files
            for unit in string_file.units
            if self._pending(unit) and unit.source.strip()
        ]
        eligible = sum(self._wanted(unit) for unit in written)
        if len(written) > eligible:
            _log.info(
                "%d line(s) are not written in %s and stay as they are.",
                len(written) - eligible,
                self.context.source_lang,
            )
        return eligible if limit is None else min(eligible, max(limit, 0))

    def _plan(self, string_files: list[StringFile]) -> None:
        """Which units this run takes, and the queues their regions are asked from."""
        self.todo_by_file: list[tuple[StringFile, list[TextUnit]]] = []
        remaining = self.stats.total
        for string_file in string_files:
            candidates = [
                unit for unit in string_file.units if self._wanted(unit) and unit.source.strip()
            ]
            todo = candidates[:remaining]
            remaining -= len(todo)
            self.todo_by_file.append((string_file, todo))
        self.before = {
            id(unit): (unit.target, unit.status)
            for _, todo in self.todo_by_file
            for unit in todo
        }
        self.regions: dict[int, str] = {}
        self.owners: dict[int, StringFile] = {}
        for string_file, todo in self.todo_by_file:
            for unit in todo:
                self.regions[id(unit)] = self._region(unit)
                self.owners[id(unit)] = string_file
        for region in self.regions.values():
            if not has_word(region):
                self.known.setdefault(region, region)
        self.attempted_regions = {
            region for region in self.regions.values() if region in self.known
        }
        self.queues = [deque(group) for group in self._ask_groups() if group]
        self.queue_of = {id(unit): queue for queue in self.queues for unit in queue}

    def _ask_groups(self) -> list[list[TextUnit]]:
        """One unit per region still to ask, grouped by file unless the engine shares them."""
        asked: set[str] = set()
        ask = []
        for _, todo in self.todo_by_file:
            for unit in todo:
                region = self.regions[id(unit)]
                if region in self.known or region in asked:
                    continue
                asked.add(region)
                ask.append(unit)
        if self.engine == "livemaker":
            return [ask]
        return [
            [unit for unit in ask if self.owners[id(unit)] is string_file]
            for string_file, _ in self.todo_by_file
        ]

    def _take(self, queue: deque[TextUnit]) -> _BatchTask:
        if self.token_counter is None:
            token_batch = TokenBatch(
                tuple(queue.popleft() for _ in range(min(self.batch_size, len(queue)))), 0
            )
        else:
            token_batch = take_token_batch(
                queue,
                self.context,
                self.glossary,
                max_units=self.batch_size,
                max_input_tokens=self.budget,
                counter=self.token_counter,
            )
            for _ in token_batch.units:
                queue.popleft()
        return _prepare_batch(
            self.owners, token_batch, self.context, self.interface.context_units
        )

    def _next_round(self) -> list[_BatchTask]:
        """As many batches as there are slots, from the first queues not empty."""
        group: list[_BatchTask] = []
        for queue in self.queues:
            while queue and len(group) < self.slots:
                group.append(self._take(queue))
        if group and not self.setup_logged:
            system, _ = build_prompt(list(group[0].regions), group[0].context, self.glossary)
            log_translation_setup(system)
            self.setup_logged = True
        return group

    def _publish(
        self,
        stage: str,
        completed: int,
        detail: str | None = None,
        **metrics: object,
    ) -> None:
        self.sink.publish(
            ProgressEvent(
                "translate",
                completed,
                self.stats.total,
                stage,
                detail=detail,
                metrics={**self.stats.snapshot(completed, self.clock()), **metrics},
                transient=stage == "translate.streaming",
            )
        )

    def _settled_line(self, unit: TextUnit, region: str, answer: str | None) -> str | None:
        """``answer`` laid back into the lines around ``unit``'s region, if it fits them."""
        if answer is None or not answer.strip():
            return None
        layout, _ = source_layout(unit.source, self.context.source_lang, self.engine)
        return (
            apply_layout(layout, answer)
            or reflow(layout, answer, region, markup_patterns(self.engine))
            or (
                None
                if any(part.literal and part.literal.strip() for part in layout.lines)
                else answer
            )
        )

    def _problems(self, unit: TextUnit, region: str, line: str | None) -> list[str]:
        if not line:
            return ["no answer came back"]
        layout, _ = source_layout(unit.source, self.context.source_lang, self.engine)
        return format_issues(
            region,
            layout_content(layout, line) or line,
            self.reject_echo,
            self.engine,
            self.target_lang,
            unit.kind == "script",
        )

    def _reject(
        self,
        string_file: StringFile,
        unit: TextUnit,
        region: str,
        answer: str | None,
        line: str | None,
        problems: list[str],
    ) -> None:
        reason = "; ".join(problems)
        if answer is not None:
            evidence = f" -- the model answered {_quoted(answer)}"
        elif (failure := self.unanswered.get(region)) is not None:
            evidence = f" -- {bounded_diagnostic(str(failure))}"
            reason += evidence
        else:
            evidence = ""
        pair = {
            "source": unit.source,
            "answer": sanitize_diagnostic(line or answer or ""),
            "marks": problem_marks(
                unit.source,
                line,
                self.reject_echo,
                self.engine,
                self.target_lang,
                unit.kind == "script",
            )
            if line
            else [],
            "reason": reason,
        }
        _log.error(
            "%s %s rejected: %s%s",
            string_file.source_file,
            unit.id,
            "; ".join(problems),
            evidence,
            extra={
                "translation_pairs": [pair],
                "pairs_heading": f"{string_file.source_file} {unit.id} rejected",
            },
        )

    def _settle_unit(self, string_file: StringFile, unit: TextUnit) -> bool:
        """Take ``unit``'s answer as its draft, or say why not; whether it became one."""
        region = self.regions[id(unit)]
        answer = self.known.get(region, self.handed_back.get(region))
        line = self._settled_line(unit, region, answer)
        problems = self._problems(unit, region, line)
        if not problems:
            _keep_overwritten(unit)
            unit.target = line
            record_transition(unit, Status.MT)
            unit.status = Status.MT
            self.stats.translated += 1
            return True
        if region in self.failed_whole:
            failure = bounded_diagnostic(str(self.unanswered[region]))
            self.unanswered_by_failure[failure] += 1
        else:
            self._reject(string_file, unit, region, answer, line, problems)
        self.stats.failed += 1
        return False

    def _settle_ready(self, *, final: bool = False) -> None:
        """Settle every unit whose region is answered, or all of them when ``final``."""
        for string_file, todo in self.todo_by_file:
            drafts: list[TextUnit] = []
            for unit in todo:
                key = id(unit)
                if key in self.processed_units:
                    continue
                region = self.regions[key]
                if not final and region not in self.known:
                    continue
                if region not in self.attempted_regions:
                    self.never_sent += 1
                    continue
                if self._settle_unit(string_file, unit):
                    drafts.append(unit)
                self.processed_units.add(key)
                self.stats.processed += 1
            if drafts:
                self.stats.translated -= _merge_drafts(
                    self.project, string_file.source_file, drafts, self.before
                )
                self.saved_files.add(string_file.source_file)
                self._publish(
                    "translate.batch.saved", self.stats.processed, string_file.source_file
                )

    def _show_reply(self, chars: int) -> None:
        self._publish("translate.streaming", self.shown_processed, streamed_chars=chars)

    def _judge_answer(self, source: str, answer: str, script: bool) -> tuple[str, list[str]]:
        """Why the run would turn ``answer`` away, and the parts that are wrong."""
        settled = settle_answer(answer, self.target_lang, self.engine)
        problems = format_issues(
            source, settled, self.reject_echo, self.engine, self.target_lang, script
        )
        if not problems:
            return "", []
        marks = problem_marks(
            source, settled, self.reject_echo, self.engine, self.target_lang, script
        )
        return "; ".join(problems), marks

    def _send(
        self, item: tuple[int, _BatchTask]
    ) -> list[str | ProviderFailure] | ProviderFailure:
        lane_index, task = item
        scripts = {region.source for region in task.regions if region.kind == "script"}
        try:
            with judging_replies(
                lambda text, reply: self._judge_answer(text, reply, text in scripts)
            ):
                return _translate_batch(
                    self.lanes,
                    lane_index,
                    list(task.regions),
                    task.context,
                    self.glossary,
                    input_tokens=task.input_tokens,
                    token=self.token,
                    sleep=self.sleep,
                )
        except ProviderFailure as exc:
            return exc

    def _send_whole(
        self, item: tuple[int, _BatchTask]
    ) -> list[str | ProviderFailure] | ProviderFailure:
        with replies_shown_whole():
            return self._send(item)

    def _send_round(
        self, group: list[_BatchTask]
    ) -> list[list[str | ProviderFailure] | ProviderFailure]:
        for task in group:
            _log.info(
                "Task: %d unit(s), %d input token(s) from %s",
                len(task.units),
                task.input_tokens,
                ", ".join(task.source_files),
            )
        work = [(index % len(self.lanes), task) for index, task in enumerate(group)]
        self.stats.lanes = max(self.stats.lanes, len(work))
        if len(work) > 1:
            with ThreadPoolExecutor(max_workers=len(work)) as pool:
                return list(pool.map(self._send_whole, work))
        return [self._send(item) for item in work]

    def _task_failed(self, task: _BatchTask, failure: ProviderFailure) -> None:
        for region in task.regions:
            self.unanswered[region.source] = failure
            self.failed_whole.add(region.source)
        self.consecutive_failures += 1
        if self.consecutive_failures >= CONSECUTIVE_BATCH_FAILURES:
            self.fatal_error = failure
            return
        _log.warning(
            "Task from %s gave up after %d attempt(s); leaving %d line(s) unanswered: %s",
            ", ".join(task.source_files),
            BATCH_ATTEMPTS,
            len(task.units),
            bounded_diagnostic(str(failure)),
        )

    def _task_answered(
        self, task: _BatchTask, answer: list[str | ProviderFailure]
    ) -> list[TextUnit]:
        """Keep what ``task`` got back; return the units to ask again."""
        self.consecutive_failures = 0
        skipped = (
            set(_skipped_lines(task.regions, answer, self.target_lang, self.engine))
            if self.reject_echo
            else set()
        )
        again: list[TextUnit] = []
        for index, (unit, region, line) in enumerate(
            zip(task.units, task.regions, answer, strict=True)
        ):
            if isinstance(line, ProviderFailure):
                self.unanswered[region.source] = line
            elif index in skipped and self.times_handed_back[region.source] < HANDED_BACK_RETRIES:
                self.times_handed_back[region.source] += 1
                self.handed_back[region.source] = line
                again.append(unit)
            else:
                self.known[region.source] = line
        given_up = sum(isinstance(line, ProviderFailure) for line in answer)
        _log.info(
            "Task returned %d translation(s)%s",
            len(answer) - given_up,
            f"; {given_up} line(s) given no answer" if given_up else "",
        )
        if again:
            _log.info(
                "%d of %d line(s) came back untranslated or in another language; "
                "asking again with the next batch",
                len(again),
                len(task.units),
            )
        return again

    def _record_round(
        self,
        group: list[_BatchTask],
        outcomes: list[list[str | ProviderFailure] | ProviderFailure],
    ) -> None:
        self.stats.batches += len(group)
        self.stats.estimated_input_tokens += sum(task.input_tokens for task in group)
        for lane in self.lanes:
            consume_usage = getattr(lane.translator, "consume_usage", None)
            if callable(consume_usage):
                self.stats.usage += consume_usage()
        for task in group:
            self.attempted_regions.update(region.source for region in task.regions)
        returned: list[TextUnit] = []
        for task, answer in zip(group, outcomes, strict=True):
            if isinstance(answer, ProviderFailure):
                self._task_failed(task, answer)
            else:
                returned.extend(self._task_answered(task, answer))
        for unit in reversed(returned):
            self.queue_of[id(unit)].appendleft(unit)
        self.shown_processed = sum(
            region in self.attempted_regions
            and (region in self.known or region not in self.handed_back)
            for region in self.regions.values()
        )
        self._publish(
            "translate.batch.completed",
            self.shown_processed,
            ", ".join(dict.fromkeys(
                source_file for task in group for source_file in task.source_files
            )),
        )

    def _report_what_was_left(self) -> None:
        if self.fatal_error is not None:
            _log.error(
                "Translation stopped: %d task(s) in a row failed. The provider said: %s",
                CONSECUTIVE_BATCH_FAILURES,
                bounded_diagnostic(str(self.fatal_error)),
            )
        self._settle_ready(final=True)
        if self.never_sent:
            _log.warning(
                "%d line(s) were never sent; they stay untranslated for the next run.",
                self.never_sent,
            )
        for failure, count in self.unanswered_by_failure.items():
            _log.error(
                "%d line(s) got no answer; they stay untranslated for the next run: %s",
                count,
                failure,
            )

    def run(self, limit: int | None, token: CancellationToken) -> TranslationResult:
        self._log_setup()
        string_files = self.project.load_string_files()
        total = self._count(string_files, limit)
        self.stats = _TranslationStats(total=total, lanes=0, started_at=self.clock())
        self.token = _PausedTimeExcluded(token, self.stats, self.clock)
        self._publish("translate.started", 0)
        self._plan(string_files)
        self._settle_ready()
        replies = _ReplyProgress(self._show_reply, self.clock)
        _listen_to_replies(self.lanes, replies.received)
        try:
            while True:
                self.token.raise_if_cancelled()
                group = self._next_round()
                if not group:
                    break
                outcomes = self._send_round(group)
                replies.clear()
                self._record_round(group, outcomes)
                self._settle_ready()
                if self.fatal_error is not None:
                    break
        finally:
            _listen_to_replies(self.lanes, None)
        self._report_what_was_left()
        if self.fatal_error is not None:
            raise self.fatal_error
        for string_file, _ in self.todo_by_file:
            if string_file.source_file not in self.saved_files:
                self._publish(
                    "translate.batch.saved", self.stats.processed, string_file.source_file
                )
        _log.info(
            "Translated %d unit(s); %d failed the format check",
            self.stats.translated,
            self.stats.failed,
        )
        return self.stats.result(self.clock())


def translate_project(
    project: Project,
    translator: Translator | None = None,
    limit: int | None = None,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    token_counter: PromptTokenCounter | None = None,
    request_limiter: RequestLimiter | None = None,
    sleep: Callable[[float], None] = time.sleep,
    lanes: Sequence[TranslationLane] | None = None,
    clock: Callable[[], float] = time.monotonic,
    redo: bool = False,
) -> TranslationResult:
    """Translate only untranslated units, saving after every group of batches."""
    if lanes is None:
        if translator is None:
            raise ValidationFailure("Translation needs a translator or a lane.")
        lanes = (TranslationLane(translator, request_limiter),)
    run = _TranslationRun(
        project,
        lanes,
        token_counter=token_counter,
        sink=sink,
        sleep=sleep,
        clock=clock,
        redo=redo,
    )
    return run.run(limit, token)


def transform_project(
    project: Project,
    pipeline: Sequence[Transform],
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
) -> TransformResult:
    """Apply an ordered idempotent transform pipeline to unprotected targets."""
    changed = 0
    paths = string_paths(project.strings_dir)
    for index, path in enumerate(paths, start=1):
        with held_string_file(path) as string_file:
            file_changed = 0
            for transform in pipeline:
                token.raise_if_cancelled()
                file_changed += transform.apply(
                    string_file.units, markup_patterns(project.config.engine.type)
                )
            if file_changed:
                project.save_string_file(string_file, path.name)
                changed += file_changed
        sink.publish(
            ProgressEvent(
                "transform",
                index,
                len(paths),
                "transform.file.completed",
                detail=string_file.source_file,
            )
        )
    return TransformResult(changed, len(pipeline))


def _held_to_todays_checks(unit: TextUnit, project: Project) -> str | None:
    """The target a machine draft keeps under today's checks, None if it goes back."""
    config = project.config
    layout, region = source_layout(unit.source, config.source_lang, config.engine.type)
    target = layout_content(layout, unit.target)
    if target is None:
        layout, region = split_layout(unit.source)
        target = split_layout(unit.target)[1]
    if not region:
        return unit.target
    settled = settle_answer(target, config.target_lang, config.engine.type)
    if format_warnings(region, settled):
        settled = (
            reflow(
                split_layout(region)[0], split_layout(settled)[1], region,
                markup_patterns(config.engine.type),
            )
            or settled
        )
    if format_issues(
        region,
        settled,
        config.source_lang != config.target_lang,
        config.engine.type,
        config.target_lang,
        unit.kind == "script",
    ):
        return None
    restored = apply_layout(layout, settled)
    if restored is None and any(part.literal and part.literal.strip() for part in layout.lines):
        return None
    return restored or settled


def recheck_report(project: Project, string_files: Sequence[StringFile]) -> RecheckResult:
    """What :func:`recheck_drafts` would do to the drafts handed in, moving none."""
    settled = sent_back = 0
    for string_file in string_files:
        for unit in string_file.units:
            if unit.status is not Status.MT:
                continue
            kept = _held_to_todays_checks(unit, project)
            if kept is None:
                sent_back += 1
            elif kept != unit.target:
                settled += 1
    return RecheckResult(settled, sent_back)


def recheck_drafts(
    project: Project,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
) -> RecheckResult:
    """Hold every machine draft to the checks an answer passes today."""
    settled = sent_back = 0
    paths = string_paths(project.strings_dir)
    for index, path in enumerate(paths, start=1):
        token.raise_if_cancelled()
        with held_string_file(path) as string_file:
            changed = False
            for unit in string_file.units:
                if unit.status is not Status.MT:
                    continue
                kept = _held_to_todays_checks(unit, project)
                if kept is None:
                    record_transition(unit, Status.UNTRANSLATED)
                    unit.status = Status.UNTRANSLATED
                    unit.target = ""
                    sent_back += 1
                elif kept != unit.target:
                    unit.target = kept
                    settled += 1
                else:
                    continue
                changed = True
            if changed:
                project.save_string_file(string_file, path.name)
        sink.publish(
            ProgressEvent(
                "recheck",
                index,
                len(paths),
                "recheck.file.completed",
                detail=string_file.source_file,
            )
        )
    return RecheckResult(settled, sent_back)


def import_translations(
    project: Project,
    mapping: Mapping[str, str],
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
) -> ImportResult:
    """Import source-target pairs as machine translations."""
    invalid_items = any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in mapping.items()
    )
    if invalid_items:
        raise ValidationFailure('Expected a JSON object of {"source": "target"}.')
    imported = 0
    matched_sources: set[str] = set()
    paths = string_paths(project.strings_dir)
    for index, path in enumerate(paths, start=1):
        token.raise_if_cancelled()
        with held_string_file(path) as string_file:
            changed = False
            for unit in string_file.units:
                target = mapping.get(unit.source)
                if not target:
                    continue
                matched_sources.add(unit.source)
                if unit.is_protected:
                    continue
                if unit.target != target or unit.status is not Status.MT:
                    unit.target = target
                    record_transition(unit, Status.MT)
                    unit.status = Status.MT
                    imported += 1
                    changed = True
            if changed:
                project.save_string_file(string_file, path.name)
        sink.publish(
            ProgressEvent(
                "import",
                index,
                len(paths),
                "import.file.completed",
                detail=string_file.source_file,
            )
        )
    unmatched = sum(
        bool(target) and source not in matched_sources
        for source, target in mapping.items()
    )
    return ImportResult(imported, unmatched)
