"""Settling a game's proper nouns before any of its lines are translated."""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressSink,
)
from bokucho.application.errors import ProviderFailure
from bokucho.application.text import settle_answer
from bokucho.application.translation_limits import (
    CONSECUTIVE_BATCH_FAILURES,
    PromptTokenCounter,
    TokenBatch,
    TranslationLane,
    request_token_budget,
    send_with_retries,
    token_batches,
)
from bokucho.model import TextUnit
from bokucho.project import Project, TranslatorInterfaceConfig
from bokucho.qa import foreign_text, has_word, source_layout
from bokucho.translate.glossary import Glossary, GlossaryEntry
from bokucho.translators.base import (
    TranslationContext,
    TranslationResponseError,
    build_prompt,
    load_json_payload,
)

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()
_log = logging.getLogger(__name__)

TERMS_SYSTEM_PROMPT = """\
You are building a translation glossary for a game, from {source_lang} to \
{target_lang}.

Read the lines you are given and collect the proper nouns in them: character \
names, place names, organisations, and invented terms that will appear again.

Rules:
- Give the rendering you would use every single time that name appears.
- Collect a name even when it sits inside brackets or a speaker label.
- Do not include ordinary words, punctuation, control codes or variables.
- Each name once, however many lines it appeared on.
- A name built on one already decided below must agree with it.
{glossary_section}
Return ONLY a JSON object mapping each name to its rendering, like
{{"name in {source_lang}": "name in {target_lang}"}}, or {{}} when the lines \
carry no proper noun. No commentary."""

TERMS_USER_PROMPT = "{entries_json}"

TERMS_REQUEST_OPTIONS = {"temperature": 0}


@dataclass(frozen=True, slots=True)
class GlossaryBuildResult:
    examined: int
    proposed: int
    added: int
    kept: int


def parse_terms(text: str) -> dict[str, str]:
    """The ``name -> rendering`` object in one answer."""
    payload = load_json_payload(text.strip().removeprefix("```json").removeprefix("```").strip("`"))
    if not isinstance(payload, dict):
        raise TranslationResponseError(
            f"Expected a JSON object of names, got: {text[:200]!r}"
        )
    return {
        source.strip(): target.strip()
        for source, target in payload.items()
        if isinstance(source, str)
        and isinstance(target, str)
        and source.strip()
        and target.strip()
        and source.strip() != target.strip()
    }


def _read_terms(lane: TranslationLane, system: str, user: str) -> dict[str, str]:
    """One task's names, as a failure the retry loop understands."""
    try:
        return parse_terms(
            lane.translator.complete(system, user, **TERMS_REQUEST_OPTIONS)
        )
    except (TranslationResponseError, json.JSONDecodeError) as exc:
        raise ProviderFailure(f"Provider returned unusable names: {exc}") from None


def _terms_context(project: Project) -> TranslationContext:
    return TranslationContext(
        source_lang=project.config.source_lang,
        target_lang=project.config.target_lang,
        system_prompt=TERMS_SYSTEM_PROMPT,
        user_prompt=TERMS_USER_PROMPT,
        engine=project.config.engine.type,
    )


def _distinct_regions(project: Project, context: TranslationContext) -> list[TextUnit]:
    """Each unit's text once, stripped of its layout, when it holds a word."""
    seen: set[str] = set()
    units = []
    for string_file in project.load_string_files():
        for unit in string_file.units:
            region = source_layout(unit.source, context.source_lang, context.engine)[1]
            if region in seen or not has_word(region):
                continue
            seen.add(region)
            units.append(unit.model_copy(update={"source": region}))
    return units


def _term_batches(
    units: list[TextUnit],
    context: TranslationContext,
    interface: TranslatorInterfaceConfig,
    token_counter: PromptTokenCounter | None,
) -> Sequence[TokenBatch]:
    if token_counter is None:
        size = interface.batch_size or 1
        return tuple(
            TokenBatch(tuple(units[start : start + size]), 0)
            for start in range(0, len(units), size)
        )
    return token_batches(
        units,
        context,
        None,
        max_units=interface.batch_size,
        max_input_tokens=request_token_budget(interface.max_request_tokens, interface.tpm),
        counter=token_counter,
    )


def _tally(
    answer: dict[str, str],
    context: TranslationContext,
    votes: dict[str, Counter[str]],
    decided: dict[str, str],
) -> None:
    """Count one task's renderings; the first reading of a name guides the tasks after it."""
    for source, rendering in answer.items():
        target = settle_answer(rendering, context.target_lang, context.engine)
        if not target or foreign_text(source, target, context.target_lang):
            _log.warning(
                "Left out %s -> %s: not written in %s",
                source,
                rendering,
                context.target_lang,
            )
            continue
        readings = votes.setdefault(source, Counter())
        readings[target] += 1
        decided.setdefault(source, readings.most_common(1)[0][0])


def _settle(glossary: Glossary, votes: dict[str, Counter[str]]) -> int:
    """Add each new name's most common rendering; a name already decided is left alone."""
    settled = {entry.source for entry in glossary.entries}
    added = 0
    for source, readings in sorted(votes.items()):
        if source in settled:
            continue
        target, _ = readings.most_common(1)[0]
        glossary.entries.append(GlossaryEntry(source=source, target=target))
        added += 1
    return added


def build_glossary(
    project: Project,
    lanes: Sequence[TranslationLane],
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    token_counter: PromptTokenCounter | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> GlossaryBuildResult:
    """Read the whole script for proper nouns and settle one rendering each."""
    interface = project.config.translator.selected_interface()
    context = _terms_context(project)
    units = _distinct_regions(project, context)
    batches = _term_batches(units, context, interface, token_counter)
    _log.info(
        "Reading %d entrie(s) for proper nouns in %d task(s), %d lane(s)",
        len(units),
        len(batches),
        len(lanes),
    )
    glossary = Glossary.load_or_empty(project.glossary_path)
    decided = glossary.as_mapping()
    votes: dict[str, Counter[str]] = {}
    consecutive_failures = 0
    for index, batch in enumerate(batches):
        token.raise_if_cancelled()
        system, user = build_prompt(list(batch.units), context, decided)
        try:
            answer = send_with_retries(
                lanes,
                index % len(lanes),
                lambda lane, s=system, u=user: _read_terms(lane, s, u),
                input_tokens=batch.input_tokens,
                token=token,
                sleep=sleep,
            )
        except ProviderFailure as exc:
            consecutive_failures += 1
            if consecutive_failures >= CONSECUTIVE_BATCH_FAILURES:
                raise
            _log.warning(
                "Task %d of %d left unread (%d line(s)): %s",
                index + 1,
                len(batches),
                len(batch.units),
                exc,
            )
            continue
        consecutive_failures = 0
        _tally(answer, context, votes, decided)
        sink.publish(
            ProgressEvent(
                "glossary",
                index + 1,
                len(batches),
                "glossary.task.read",
                detail=f"{len(votes)} term(s)",
            )
        )
    added = _settle(glossary, votes)
    if added:
        glossary.save(project.glossary_path)
    kept = len(votes) - added
    _log.info(
        "Settled %d name(s); %d already decided were left alone", added, kept
    )
    return GlossaryBuildResult(len(units), len(votes), added, kept)
