"""Token-safe batching and request-throttling contracts for LLM translation."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from bokucho.application.contracts import CancellationToken
from bokucho.application.errors import (
    ProviderFailure,
    ProviderRateLimited,
    ProviderRefused,
)
from bokucho.model import TextUnit
from bokucho.translators.base import TranslationContext, Translator

_log = logging.getLogger(__name__)


class PromptTokenCounter(Protocol):
    def count(
        self,
        batch: list[TextUnit],
        context: TranslationContext,
        glossary: Mapping[str, str] | None,
    ) -> int: ...


class RequestLimiter(Protocol):
    def acquire(
        self,
        input_tokens: int,
        cancellation: CancellationToken,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TokenBatch:
    units: tuple[TextUnit, ...]
    input_tokens: int


@dataclass(frozen=True, slots=True)
class TranslationLane:
    """One credential: its own client and its own share of the quota."""

    translator: Translator
    limiter: RequestLimiter | None = None


BATCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
CONSECUTIVE_BATCH_FAILURES = 3
HANDED_BACK_RETRIES = 2
RATE_LIMIT_ATTEMPTS = 6
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 30.0
MAX_RATE_LIMIT_WAIT_SECONDS = 60.0


DIAGNOSTIC_LIMIT = 200
_SECRET_ASSIGNMENT = re.compile(
    r"(?ix)"
    r"(?P<label>\b(?:api[\s_-]?key|access[\s_-]?token|refresh[\s_-]?token|"
    r"authorization|credentials?|password|secret|token)\b\s*(?:[:=]|\bis\b)\s*)"
    r"(?:bearer\s+)?"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;\]}]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_SECRET_COMPOUND = re.compile(
    r"(?i)\b(?:credential|password|secret|token)[_-][a-z0-9][a-z0-9._~-]{5,}"
)
_KNOWN_SECRET = re.compile(
    r"(?x)\b(?:"
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{8,}|"
    r"gh[pousr]_[A-Za-z0-9]{8,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")\b"
)


def sanitize_diagnostic(value: str) -> str:
    """Redact credential-shaped material without consulting outer adapters."""
    sanitized = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group('label')}[REDACTED]", value
    )
    sanitized = _BEARER_VALUE.sub("Bearer [REDACTED]", sanitized)
    sanitized = _SECRET_COMPOUND.sub("[REDACTED]", sanitized)
    sanitized = _KNOWN_SECRET.sub("[REDACTED]", sanitized)
    return sanitized


def bounded_diagnostic(value: str) -> str:
    """Sanitized provider detail as one bounded log-record value."""
    one_line = " ".join(sanitize_diagnostic(value).splitlines())
    if len(one_line) <= DIAGNOSTIC_LIMIT:
        return one_line
    return f"{one_line[: DIAGNOSTIC_LIMIT - 3]}..."


def send_with_retries[T](
    lanes: Sequence[TranslationLane],
    lane_index: int,
    call: Callable[[TranslationLane], T],
    *,
    input_tokens: int,
    token: CancellationToken,
    sleep: Callable[[float], None],
) -> T:
    """Spend a lane on ``call``, moving to the next one when it comes back bad."""
    attempt = refusals = 0
    rotation = 0
    while True:
        token.raise_if_cancelled()
        lane = lanes[(lane_index + rotation) % len(lanes)]
        rotation += 1
        if lane.limiter is not None:
            lane.limiter.acquire(input_tokens, token)
        try:
            return call(lane)
        except ProviderRefused:
            raise
        except ProviderRateLimited as exc:
            refusals += 1
            if refusals >= RATE_LIMIT_ATTEMPTS * len(lanes):
                raise
            if refusals % len(lanes) == 0:
                wait = min(
                    exc.retry_after or DEFAULT_RATE_LIMIT_WAIT_SECONDS,
                    MAX_RATE_LIMIT_WAIT_SECONDS,
                )
                _log.warning(
                    "Every lane is rate limited; waiting %.0fs before asking again.",
                    wait,
                )
                sleep(wait)
        except ProviderFailure as exc:
            attempt += 1
            if attempt >= BATCH_ATTEMPTS:
                raise
            _log.warning(
                "Task failed (attempt %d of %d), asking again: %s",
                attempt,
                BATCH_ATTEMPTS,
                bounded_diagnostic(str(exc)),
            )
            sleep(RETRY_BACKOFF_SECONDS * attempt)


def request_token_budget(max_request_tokens: int, tpm: int) -> int:
    """The most input tokens one request may carry: no request over TPM can ever be sent."""
    return min((limit for limit in (max_request_tokens, tpm) if limit > 0), default=0)


def take_token_batch(
    units: Iterable[TextUnit],
    context: TranslationContext,
    glossary: Mapping[str, str] | None,
    *,
    max_units: int,
    max_input_tokens: int,
    counter: PromptTokenCounter,
) -> TokenBatch:
    """The first batch :func:`token_batches` would make, reading no further."""
    current: list[TextUnit] = []
    current_tokens = 0
    for unit in units:
        candidate = [*current, unit]
        candidate_tokens = counter.count(candidate, context, glossary)
        exceeds_units = max_units > 0 and len(candidate) > max_units
        exceeds_tokens = max_input_tokens > 0 and candidate_tokens > max_input_tokens
        if current and (exceeds_units or exceeds_tokens):
            break
        current = candidate
        current_tokens = candidate_tokens
    return TokenBatch(tuple(current), current_tokens)


def token_batches(
    units: Sequence[TextUnit],
    context: TranslationContext,
    glossary: Mapping[str, str] | None,
    *,
    max_units: int,
    max_input_tokens: int,
    counter: PromptTokenCounter,
) -> tuple[TokenBatch, ...]:
    """Greedily group whole units under the rendered prompt token budget."""
    batches: list[TokenBatch] = []
    current: list[TextUnit] = []
    current_tokens = 0

    for unit in units:
        candidate = [*current, unit]
        candidate_tokens = counter.count(candidate, context, glossary)
        exceeds_units = max_units > 0 and len(candidate) > max_units
        exceeds_tokens = (
            max_input_tokens > 0 and candidate_tokens > max_input_tokens
        )
        if current and (exceeds_units or exceeds_tokens):
            batches.append(TokenBatch(tuple(current), current_tokens))
            current = [unit]
            current_tokens = counter.count(current, context, glossary)
        else:
            current = candidate
            current_tokens = candidate_tokens

    if current:
        batches.append(TokenBatch(tuple(current), current_tokens))
    return tuple(batches)
