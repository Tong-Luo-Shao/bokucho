"""Application logging: one console line per event, optionally kept on disk."""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import unicodedata
from collections.abc import Callable, Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

ROOT_LOGGER = "bokucho"
MAX_LOG_BYTES = 5 * 1024 * 1024
_CLOCK_WIDTH = 10
_LEVEL_WIDTH = 8
_TRUNCATION_MARKER = " ... [truncated]"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"
_WHITE = "\x1b[37m"
_RESET = "\x1b[0m"
_PAIR_SEPARATOR = "─" * 40
_CONTROL = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def render_translation_pairs(pairs: list[dict], *, color: bool = False) -> str:
    """Each line's source, ``>``, the answer, and why it failed if it did."""

    def paint(text: str, colour: str) -> str:
        return f"{colour}{text}{_RESET}" if color else text

    blocks = []
    for pair in pairs:
        reason = pair.get("reason", "")
        answer = pair["answer"]
        if color and reason:
            for mark in dict.fromkeys(pair.get("marks", ())):
                if mark:
                    answer = answer.replace(mark, paint(mark, _RED))
        lines = pair["source"].split("\n")
        marker = paint(
            ">", (_RED if reason else _GREEN) if pair.get("judged", True) else _WHITE
        )
        lines.append(marker)
        lines.append(answer)
        if reason:
            lines.append(paint(reason, _RED))
        lines.append(_PAIR_SEPARATOR)
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def display_width(text: str) -> int:
    """Columns ``text`` takes on a terminal: wide characters take two."""
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in text
    )


Judge = Callable[[str, str], tuple[str, Sequence[str]]]


class LivePairs:
    """Each line of a batch, ``>``, and its answer, printed as the reply arrives."""

    REDRAW_INTERVAL = 0.05

    def __init__(
        self,
        sources: Sequence[str],
        logger: logging.Logger,
        *,
        live: bool,
        judge: Judge | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sources = sources
        self._logger = logger
        self._judge = judge
        self._clock = clock
        self._console = _console_handler() if live else None
        formatter = self._console.formatter if self._console is not None else None
        self._in_place = bool(getattr(formatter, "color", False))
        self._shown = 0
        self._partial = ""
        self._region_rows = 0
        self._drawn_at: float | None = None
        self._stale = False
        self._open = False
        self._written = ""
        if self._console is not None and self._sources:
            if self._in_place:
                self._draw()
            else:
                self._open_line()

    def show(self, done: Sequence[str], partial: str | None) -> None:
        while self._shown < len(done):
            pair = self._judged(done[self._shown])
            if self._console is not None:
                if self._in_place:
                    self._settle(pair)
                else:
                    self._close_line(pair)
            self._log_pair(pair)
            self._shown += 1
            if self._console is not None and not self._in_place:
                if self._shown < len(self._sources):
                    self._open_line()
        if partial is not None and self._console is not None:
            if self._in_place:
                if partial != self._partial:
                    self._partial = partial
                    self._draw(soon=True)
            else:
                self._extend(partial)

    def finish(self) -> None:
        """Draw what is left to draw; end a line left open."""
        if self._in_place:
            if self._stale:
                self._draw()
            return
        if self._open and self._written:
            self._write("\n")
        self._open = False

    def _source(self) -> str:
        return self._sources[self._shown] if self._shown < len(self._sources) else ""

    def _judged(self, answer: str) -> dict:
        reason, marks = self._judge(self._source(), answer) if self._judge else ("", ())
        return {
            "source": self._source(),
            "answer": answer,
            "marks": list(marks),
            "reason": reason,
            "judged": self._judge is not None,
        }

    def _settle(self, pair: dict) -> None:
        """Draw a finished line judged, and leave it above what is redrawn."""
        block = render_translation_pairs([pair], color=True) + "\n"
        self._draw(first=block)
        self._region_rows = max(0, self._region_rows - self._rows(block))
        self._partial = ""

    def _draw(self, *, first: str | None = None, soon: bool = False) -> None:
        now = self._clock()
        if soon and self._drawn_at is not None and now - self._drawn_at < self.REDRAW_INTERVAL:
            self._stale = True
            return
        limit = self._screen_rows() - 1
        if first is None:
            first = self._pending(self._shown, self._partial)
        blocks = [first]
        rows = self._rows(first)
        for index in range(self._shown + 1, len(self._sources)):
            block = self._pending(index, "")
            rows += self._rows(block)
            if rows > limit:
                break
            blocks.append(block)
        if 0 < self._region_rows <= limit:
            back = f"\x1b[{self._region_rows}F\x1b[J"
        elif self._region_rows:
            back = "\n"
        else:
            back = ""
        text = "".join(blocks)
        self._write(back + text)
        self._region_rows = self._rows(text)
        self._drawn_at = now
        self._stale = False

    def _pending(self, index: int, answer: str) -> str:
        source = self._sources[index] if index < len(self._sources) else ""
        lines = [*([source] if source else []), f"{_WHITE}>{_RESET}"]
        if answer:
            lines.append(answer)
        lines.append(_PAIR_SEPARATOR)
        return "\n".join(lines) + "\n"

    def _open_line(self) -> None:
        source = self._source()
        self._write(f"{source}\n>\n" if source else ">\n")
        self._open = True
        self._written = ""

    def _extend(self, text: str) -> None:
        if not self._open:
            self._open_line()
        self._write(text[len(self._written) :] if text.startswith(self._written) else text)
        self._written = text

    def _close_line(self, pair: dict) -> None:
        self._extend(pair["answer"])
        reason = pair["reason"]
        self._write(f"\n{reason}\n{_PAIR_SEPARATOR}\n" if reason else f"\n{_PAIR_SEPARATOR}\n")
        self._open = False
        self._written = ""

    @staticmethod
    def _rows(text: str) -> int:
        """Rows ``text`` fills on the console, the lines too wide wrapping."""
        columns = max(1, shutil.get_terminal_size(fallback=(100, 24)).columns)
        lines = _CONTROL.sub("", text).split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return sum(max(1, -(-display_width(line) // columns)) for line in lines)

    @staticmethod
    def _screen_rows() -> int:
        return shutil.get_terminal_size(fallback=(100, 24)).lines

    def _write(self, text: str) -> None:
        console = self._console
        if console is None or not text:
            return
        console.acquire()
        try:
            console.stream.write(text)
            console.flush()
        except (OSError, ValueError):
            self._console = None
        finally:
            console.release()

    def _log_pair(self, pair: dict) -> None:
        self._logger.info(
            "%s",
            render_translation_pairs([pair]),
            extra={
                "ui_hidden": True,
                "translation_pairs": [pair],
                "console_hidden": self._console is not None,
            },
        )


def _console_handler() -> logging.StreamHandler | None:
    for handler in logging.getLogger(ROOT_LOGGER).handlers:
        if isinstance(handler, logging.StreamHandler) and getattr(
            handler, "bokucho_console", False
        ):
            return handler
    return None


def _not_for_the_console(record: logging.LogRecord) -> bool:
    return not getattr(record, "console_hidden", False)


class CappedLogFile(RotatingFileHandler):
    """A log file that starts over once it reaches its ceiling."""

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        mode, self.mode = self.mode, "w"
        try:
            self.stream = self._open()
        finally:
            self.mode = mode

    @staticmethod
    def _disk_text(text: str) -> str:
        return text.replace("\n", os.linesep)

    @classmethod
    def _utf8_bytes(cls, text: str) -> bytes:
        return cls._disk_text(text).encode("utf-8")

    @staticmethod
    def _logical_text(text: str) -> str:
        if os.linesep == "\n":
            return text
        return text.replace(os.linesep, "\n")

    def _bounded_record(self, formatted: str) -> tuple[str, int]:
        complete = f"{formatted}{self.terminator}"
        encoded = self._utf8_bytes(complete)
        if self.maxBytes <= 0 or len(encoded) <= self.maxBytes:
            return complete, len(encoded)

        suffix = f"{_TRUNCATION_MARKER}{self.terminator}"
        encoded_suffix = self._utf8_bytes(suffix)
        available = self.maxBytes - len(encoded_suffix)
        if available < 0:
            bounded_suffix = encoded_suffix[: self.maxBytes].decode(
                "utf-8", errors="ignore"
            )
            bounded = self._logical_text(bounded_suffix)
            return bounded, len(self._utf8_bytes(bounded))

        encoded_prefix = self._utf8_bytes(formatted)[:available]
        disk_prefix = encoded_prefix.decode("utf-8", errors="ignore")
        prefix = self._logical_text(disk_prefix)
        bounded = f"{prefix}{suffix}"
        return bounded, len(self._utf8_bytes(bounded))

    def _current_size(self) -> int:
        if self.stream:
            self.stream.flush()
        path = Path(self.baseFilename)
        return path.stat().st_size if path.exists() else 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text, encoded_size = self._bounded_record(self.format(record))
            if (
                self.maxBytes > 0
                and self._current_size() + encoded_size > self.maxBytes
            ):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            if self.stream:
                self.stream.write(text)
                self.flush()
        except Exception:  # noqa: BLE001 - a handler may never raise into its caller
            self.handleError(record)


class ConsoleFormatter(logging.Formatter):
    """``[HH:MM:SS] LEVEL    [logger.name] message``."""

    def __init__(self, *, color: bool = False) -> None:
        super().__init__()
        self._last_clock: str | None = None
        self._color = color

    @property
    def color(self) -> bool:
        return self._color

    def format(self, record: logging.LogRecord) -> str:
        clock = time.strftime("%H:%M:%S", self.converter(record.created))
        stamp = f"[{clock}]" if clock != self._last_clock else " " * _CLOCK_WIDTH
        self._last_clock = clock
        message = record.getMessage()
        pairs = getattr(record, "translation_pairs", None)
        heading = ""
        if isinstance(pairs, list):
            message = render_translation_pairs(pairs, color=self._color)
            heading = getattr(record, "pairs_heading", "")
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        prefix = f"{stamp} {record.levelname:<{_LEVEL_WIDTH}} [{record.name}] "
        if heading:
            return f"{prefix}{heading}\n{message}"
        if "\n" in message:
            return f"{prefix}\n{message}"
        return f"{prefix}{message}"


def configure(
    level: int = logging.INFO,
    *,
    stream: TextIO | None = None,
    path: Path | None = None,
) -> logging.Logger:
    """Install the console handler, and a file handler when ``path`` is given."""
    logger = logging.getLogger(ROOT_LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(stream)
    console_stream = console.stream
    isatty = getattr(console_stream, "isatty", None)
    console.setFormatter(ConsoleFormatter(color=bool(isatty and isatty())))
    console.bokucho_console = True  # type: ignore[attr-defined]
    console.addFilter(_not_for_the_console)
    logger.addHandler(console)

    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = CappedLogFile(
            path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=0,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(ConsoleFormatter())
        logger.addHandler(file_handler)

    logger.setLevel(level)
    return logger
