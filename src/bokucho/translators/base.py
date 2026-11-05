"""``Translator`` — plugin seam #2 (LLM provider)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock

from pydantic import BaseModel

from bokucho.application.errors import ProviderFailure, ProviderMiscounted
from bokucho.model import TextUnit
from bokucho.project import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
from bokucho.qa import translation_tokens

_log = logging.getLogger(__name__)


def _display_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


def _log_panel(title: str, content: str) -> str:
    console_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    content_width = max(1, console_width - 4)
    lines = []
    for source_line in content.splitlines() or ["（空）"]:
        line = ""
        line_width = 0
        for char in source_line:
            char_width = _display_width(char)
            if line and line_width + char_width > content_width:
                lines.append(line)
                line = ""
                line_width = 0
            line += char
            line_width += char_width
        lines.append(line)
    width = max(
        _display_width(title) + 4,
        min(console_width, max(_display_width(line) + 4 for line in lines)),
    )
    top = (
        f"┌─ {title} "
        + "─" * (width - _display_width(f"┌─ {title} ") - 1)
        + "┐"
    )
    body = "\n".join(
        f"│ {line}{' ' * (width - _display_width(line) - 4)} │"
        for line in lines
    )
    bottom = f"└{'─' * (width - 2)}┘"
    return f"{top}\n{body}\n{bottom}"


def render_context_section(preceding: Sequence[TextUnit]) -> str:
    """Preceding units as read-only context, or nothing when there are none."""
    if not preceding:
        return ""
    lines = []
    for unit in preceding:
        lines.append(f"  - {unit.source}")
        if unit.target.strip():
            lines.append(f"    -> {unit.target}")
    body = "\n".join(lines)
    return (
        "Context — the lines immediately before these entries, already handled.\n"
        "Do not translate or return them; they are here for continuity only.\n"
        f"{body}\n\n"
    )


def source_lines(source: str) -> list[str]:
    """``source`` as the lines it is sent and answered as."""
    return source.split("\n")


_PROTECTED_TOKENS_RULE = (
    "\n\nEach entry includes `protected_tokens`, a reference list of runtime "
    "variables and codes recognized for the configured engine. Preserve "
    "every listed token exactly as written, including its spelling and "
    "arguments. You may move tokens to a natural position in the "
    "translation, but do not translate, remove, or alter them -- not even "
    "the words inside full-width brackets when they are part of a token."
)


def build_prompt(
    batch: list[TextUnit],
    context: TranslationContext,
    glossary: Mapping[str, str] | None,
) -> tuple[str, str]:
    """(system, user) message pair shared by all LLM providers."""
    system = context.system_prompt.format(
        Orig_Lang=context.source_lang,
        Target_Lang=context.target_lang,
        source_lang=context.source_lang,
        target_lang=context.target_lang,
        glossary_section=_glossary_section(batch, glossary),
        n=len(batch),
    )
    system += (
        f"\n\nLanguage tags follow BCP 47. Source: {context.source_lang}. "
        f"Target: {context.target_lang}."
    )
    if context.source_lang.lower() == "und":
        system += (
            " The source tag 'und' means undetermined. "
            "Detect the source language of each entry before translating it "
            f"into {context.target_lang}."
        )
    system += _PROTECTED_TOKENS_RULE
    entries = [
        {
            "text": source_lines(u.source),
            "speaker": u.context.speaker,
            "scene": u.context.scene,
            "protected_tokens": translation_tokens(u.source, context.engine),
        }
        for u in batch
    ]
    user = context.user_prompt.format(
        context_section=render_context_section(context.preceding),
        entries_json=json.dumps(entries, ensure_ascii=False),
        n=len(batch),
    )
    return system, user


def _glossary_section(batch: list[TextUnit], glossary: Mapping[str, str] | None) -> str:
    """The glossary entries whose source appears somewhere in ``batch``."""
    relevant = {s: t for s, t in (glossary or {}).items() if any(s in u.source for u in batch)}
    if not relevant:
        return ""
    lines = "\n".join(f"  {s} => {t}" for s, t in relevant.items())
    return f"- Use this glossary for names and terms:\n{lines}\n"


_RETRY_SECONDS_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)


def retry_after_seconds(error: object) -> float | None:
    """The wait a provider asked for, or ``None`` when it did not say."""
    headers = getattr(getattr(error, "response", None), "headers", None)
    raw = headers.get("retry-after") if headers is not None else None
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    match = _RETRY_SECONDS_RE.search(str(error))
    return float(match.group(1)) if match else None


_FAILURE_DETAIL_LIMIT = 300


def failure_detail(error: object, secret: str | None) -> str:
    """What to say about a failed request: its type, or the provider's answer."""
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        return type(error).__name__
    body = getattr(error, "body", None)
    if isinstance(body, list) and body:
        body = body[0]
    said = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(said, dict):
        said = said.get("message")
    message = str(said or getattr(error, "message", "") or "")
    if secret:
        message = message.replace(secret, "[REDACTED]")
    from bokucho.application.translation_limits import sanitize_diagnostic

    message = " ".join(sanitize_diagnostic(message).split())
    if len(message) > _FAILURE_DETAIL_LIMIT:
        message = f"{message[: _FAILURE_DETAIL_LIMIT - 3]}..."
    return f"HTTP {status}: {message}" if message else f"HTTP {status}"


def log_prompt(logger: logging.Logger, system: str, user: str) -> None:
    """Record a prompt at the point it is sent."""
    logger.debug("Prompt sent (system):\n%s", system)
    logger.debug("Prompt sent (user):\n%s", user)


def log_translation_setup(
    system: str,
) -> None:
    """Log the system prompt once when a translation run starts."""
    if not _log.isEnabledFor(logging.INFO):
        return
    _log.info(
        "%s",
        _log_panel("系統提示字", system),
        extra={"ui_hidden": True},
    )


def log_translation_context(
    batch: Sequence[TextUnit],
    preceding: Sequence[TextUnit],
    glossary: Mapping[str, str] | None,
) -> None:
    """Log the glossary and preceding context for the current request."""
    if not _log.isEnabledFor(logging.INFO):
        return

    relevant = (
        {
            source: target
            for source, target in glossary.items()
            if any(source in unit.source for unit in batch)
        }
        if glossary
        else {}
    )
    terms = "\n".join(
        f"{source} → {target}" for source, target in relevant.items()
    ) or "（沒有相關詞語）"
    context_lines = []
    for index, unit in enumerate(preceding):
        if index:
            context_lines.append("─" * 24)
        context_lines.append(unit.source)
        if unit.target.strip():
            context_lines.append(f"→ {unit.target}")
    previous = "\n".join(context_lines) or "（沒有前句）"

    _log.info(
        "%s\n%s",
        _log_panel("詞語表", terms),
        _log_panel("作為上下文的前句", previous),
        extra={"ui_hidden": True},
    )


class TranslationResponseError(ValueError):
    """The model's answer could not be read as translations."""


class TranslationCountError(TranslationResponseError):
    """The answer is an array of translations, but not one per entry."""


_TRANSLATION_KEYS = ("text", "translation", "target")


def _as_translation(entry: object, lines: int | None = None) -> str:
    """One array item as translated text."""
    if isinstance(entry, dict):
        entry = next(
            (
                entry[key]
                for key in _TRANSLATION_KEYS
                if isinstance(entry.get(key), (str, list))
            ),
            entry,
        )
    if isinstance(entry, str):
        return entry
    if isinstance(entry, list) and all(isinstance(item, str) for item in entry):
        if lines is not None and len(entry) != lines:
            _log.warning(
                "Answer holds %d line(s) for an entry of %d", len(entry), lines
            )
        return "\n".join(entry)
    raise TranslationResponseError(f"Translation entry is not text: {entry!r:.120}")


def _escape_inner_quotes(text: str) -> str:
    """Escape the quotes inside a string that the model left bare."""
    out: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            out.append(char)
            in_string = char == '"'
            index += 1
        elif char == "\\" and index + 1 < len(text):
            out.append(text[index : index + 2])
            index += 2
        elif char == '"':
            following = text[index + 1 :].lstrip()
            if not following or following[0] in ",]}:":
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


_UNOPENED = re.compile(r'^(\s*)(?=[^\s"\[\]{},])(.*"\s*,?\s*)$', re.MULTILINE)


def _open_bare_strings(text: str) -> str:
    """Open the strings the model began without their quote."""
    return _UNOPENED.sub(r'\1"\2', text)


_THINKING = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_FENCED = re.compile(r"```(?:json)?(.*?)```", re.DOTALL | re.IGNORECASE)


def load_json_payload(text: str) -> object:
    """The JSON in ``text``, looking past whatever the model put in front of it."""
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError as first_error:
        candidates = [_escape_inner_quotes(text), _open_bare_strings(text)]
        for candidate in candidates:
            try:
                return json.loads(candidate, strict=False)
            except json.JSONDecodeError:
                continue
        decoder = json.JSONDecoder(strict=False)
        for start in _outermost_starts(text):
            try:
                return decoder.raw_decode(text, start)[0]
            except json.JSONDecodeError:
                continue
        raise first_error from None


def _outermost_starts(text: str) -> Iterator[int]:
    """Where a JSON value may start outside every bracket ``text`` has opened."""
    depth = 0
    in_string = escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character in "[{":
            if depth == 0:
                yield index
            depth += 1
        elif character in "]}":
            depth = max(depth - 1, 0)
        elif character == '"' and depth:
            in_string = True


def parse_translations(
    text: str,
    expected: int,
    line_counts: Sequence[int] | None = None,
) -> list[str]:
    """Parse a (possibly fenced) JSON-array response; validate the count."""
    _log.debug("Response received:\n%s", text)
    text = _THINKING.sub("", text).strip()
    if fenced := _FENCED.search(text):
        text = fenced.group(1)
    elif text.startswith("```"):
        text = text.split("```")[1].removeprefix("json")
    try:
        targets = load_json_payload(text)
    except json.JSONDecodeError as error:
        raise TranslationResponseError(
            f"Response is not JSON ({error}), got: {text[:200]!r}"
        ) from None
    if not isinstance(targets, list):
        raise TranslationResponseError(
            f"Expected a JSON array of {expected} translations, got: {text[:200]!r}"
        )
    if len(targets) != expected:
        raise TranslationCountError(
            f"Expected {expected} translations, got {len(targets)}: {text[:200]!r}"
        )
    lines = list(line_counts) if line_counts is not None else [None] * expected
    return [
        _as_translation(entry, count)
        for entry, count in zip(targets, lines, strict=True)
    ]


class TranslationContext(BaseModel):
    """What a batch is translated against: languages, wording, and what came before."""

    source_lang: str
    target_lang: str
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_prompt: str = DEFAULT_USER_PROMPT
    preceding: tuple[TextUnit, ...] = ()
    engine: str = ""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported tokens accumulated since the last read."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    requests: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=(
                self.cached_input_tokens + other.cached_input_tokens
            ),
            requests=self.requests + other.requests,
        )


StreamListener = Callable[[int], None]


_replying_to: ContextVar[tuple[str, ...] | None] = ContextVar("replying_to", default=None)
_shown_as_written: ContextVar[bool] = ContextVar("shown_as_written", default=True)
_judge: ContextVar[Callable[[str, str], tuple[str, Sequence[str]]] | None] = ContextVar(
    "judge", default=None
)


@contextmanager
def judging_replies(judge: Callable[[str, str], tuple[str, Sequence[str]]]) -> Iterator[None]:
    """Have each answer shown inside marked as passing or not once it is whole."""
    reset = _judge.set(judge)
    try:
        yield
    finally:
        _judge.reset(reset)


@contextmanager
def replying_to(batch: Sequence[TextUnit]) -> Iterator[None]:
    """Have the replies asked for inside show each answer beside its line."""
    reset = _replying_to.set(tuple(unit.source for unit in batch))
    try:
        yield
    finally:
        _replying_to.reset(reset)


@contextmanager
def replies_shown_whole() -> Iterator[None]:
    """Print each line once its answer is whole, not as it is written."""
    reset = _shown_as_written.set(False)
    try:
        yield
    finally:
        _shown_as_written.reset(reset)


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


class ReplyEntries:
    """The answers in a JSON array that has not finished arriving."""

    def __init__(self) -> None:
        self.done: list[str] = []
        self._stack: list[str] = []
        self._ended = False
        self._in_string = False
        self._is_key = False
        self._expect_key = False
        self._escape = False
        self._unicode: str | None = None
        self._high_surrogate: int | None = None
        self._chars: list[str] = []
        self._lines: list[str] = []

    @property
    def partial(self) -> str | None:
        if len(self._stack) < 2 and not (self._in_string and len(self._stack) == 1):
            return None
        pieces = list(self._lines)
        if self._in_string and not self._is_key:
            pieces.append("".join(self._chars))
        return "\n".join(pieces)

    def feed(self, piece: str) -> None:
        for char in piece:
            self._read(char)

    def _read(self, char: str) -> None:
        if self._ended:
            return
        if self._in_string:
            self._read_string(char)
        elif not self._stack:
            if char == "[":
                self._stack.append(char)
        elif char == '"':
            self._in_string = True
            self._is_key = self._stack[-1] == "{" and self._expect_key
            self._chars = []
        elif char in "[{":
            self._stack.append(char)
            self._expect_key = char == "{"
        elif char in "]}":
            self._stack.pop()
            if not self._stack:
                self._ended = True
            elif len(self._stack) == 1:
                self.done.append("\n".join(self._lines))
                self._lines = []
        elif char == ",":
            self._expect_key = self._stack[-1] == "{"
        elif char == ":":
            self._expect_key = False

    def _read_string(self, char: str) -> None:
        if self._unicode is not None:
            self._unicode += char
            if len(self._unicode) == 4:
                code, self._unicode = self._unicode, None
                try:
                    self._add_code(int(code, 16))
                except ValueError:
                    pass
        elif self._escape:
            self._escape = False
            if char == "u":
                self._unicode = ""
            else:
                self._chars.append(_ESCAPES.get(char, char))
        elif char == "\\":
            self._escape = True
        elif char == '"':
            self._in_string = False
            text = "".join(self._chars)
            if self._is_key:
                return
            if len(self._stack) == 1:
                self.done.append(text)
            else:
                self._lines.append(text)
        else:
            self._chars.append(char)

    def _add_code(self, code: int) -> None:
        if 0xD800 <= code < 0xDC00:
            self._high_surrogate = code
            return
        if 0xDC00 <= code < 0xE000:
            high, self._high_surrogate = self._high_surrogate, None
            if high is not None:
                self._chars.append(chr(0x10000 + ((high - 0xD800) << 10) + code - 0xDC00))
            return
        self._chars.append(chr(code))


class ReplyStream:
    """A reply read as it arrives."""

    def __init__(self, logger: logging.Logger, listener: StreamListener | None) -> None:
        self._listener = listener
        self._pieces: list[str] = []
        self._received = 0
        self._entries: ReplyEntries | None = None
        self._pairs = None
        sources = _replying_to.get()
        if sources is not None and logger.isEnabledFor(logging.INFO):
            from bokucho.logs import LivePairs

            self._entries = ReplyEntries()
            self._pairs = LivePairs(
                sources, logger, live=_shown_as_written.get(), judge=_judge.get()
            )
        if listener is not None:
            listener(0)

    def add(self, piece: str) -> None:
        if not piece:
            return
        self._pieces.append(piece)
        self._received += len(piece)
        if self._entries is not None and self._pairs is not None:
            self._entries.feed(piece)
            self._pairs.show(self._entries.done, self._entries.partial)
        if self._listener is not None:
            self._listener(self._received)

    def finish(self) -> str:
        """Everything that arrived."""
        if self._pairs is not None:
            self._pairs.finish()
        return "".join(self._pieces)


class Translator(ABC):
    """Abstract base class for LLM translation providers."""

    name: str = ""
    _stream_listener: StreamListener | None = None

    def __init__(self) -> None:
        self._usage_lock = Lock()
        self._pending_token_usage = TokenUsage()

    def _record_usage(self, usage: TokenUsage) -> None:
        with self._usage_lock:
            self._pending_token_usage += usage

    def set_stream_listener(self, listener: StreamListener | None) -> None:
        """Have each reply's progress told to ``listener``; ``None`` stops it."""
        self._stream_listener = listener

    def consume_usage(self) -> TokenUsage:
        """Return and clear usage reported by requests since the last read."""
        with self._usage_lock:
            pending = self._pending_token_usage
            self._pending_token_usage = TokenUsage()
            return pending

    @abstractmethod
    def translate(
        self,
        batch: list[TextUnit],
        context: TranslationContext,
        glossary: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Translate ``batch`` and return one target string per input unit."""
        raise NotImplementedError

    def complete(self, system: str, user: str, **options: object) -> str:
        """Answer one prompt pair and return the text, unparsed."""
        raise NotImplementedError(
            f"{type(self).__name__} answers translations only, not raw prompts."
        )


class ProviderTranslator(Translator):
    """A provider SDK that translates a batch by answering one prompt pair."""

    label: str = ""
    api_key: str | None = None
    api_key_env: str | None = None

    @abstractmethod
    def _sdk_error(self) -> type[Exception]:
        """The base class of every error the provider's SDK raises."""

    def _secret(self) -> str | None:
        return self.api_key or (os.environ.get(self.api_key_env) if self.api_key_env else None)

    def translate(
        self,
        batch: list[TextUnit],
        context: TranslationContext,
        glossary: Mapping[str, str] | None = None,
    ) -> list[str]:
        try:
            system, user = build_prompt(batch, context, glossary)
            log_translation_context(batch, context.preceding, glossary)
            with replying_to(batch):
                reply = self.complete(system, user)
            return parse_translations(
                reply,
                len(batch),
                [len(source_lines(unit.source)) for unit in batch],
            )
        except TranslationCountError as exc:
            raise ProviderMiscounted(
                f"{self.label} provider returned an unusable response: {exc}"
            ) from None
        except TranslationResponseError as exc:
            raise ProviderFailure(
                f"{self.label} provider returned an unusable response: {exc}"
            ) from None
        except (
            self._sdk_error(),
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ProviderFailure(f"{self.label} provider failed ({type(exc).__name__}).") from None
