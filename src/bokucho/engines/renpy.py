"""Ren'Py engine adapter — stands on the official SDK's ``translate`` command."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import typer

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressSink,
)
from bokucho.engines.base import EngineAdapter
from bokucho.model import Status, TextUnit

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()

_ESCAPES = {"n": "\n", "t": "\t"}


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), s)


def _escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def default_language(target_lang: str) -> str:
    """A Ren'Py tl language name derived from a BCP-47-ish tag (zh-tw -> zh_tw)."""
    return re.sub(r"[^a-z0-9]+", "_", target_lang.lower()).strip("_")


_HEADER_RE = re.compile(r"^translate\s+(\w+)\s+(\w+):\s*$")
_STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
_OLD_RE = re.compile(r'^\s*old\s+"((?:\\.|[^"\\])*)"\s*$')
_NEW_RE = re.compile(r'^\s*new\s+"((?:\\.|[^"\\])*)"\s*$')

_NON_DIALOGUE_IDS = ("strings", "python", "style")


def _block_id_from_header(line: str) -> str | None:
    """The dialogue block id in a ``translate <lang> <id>:`` line, else None."""
    header = _HEADER_RE.match(line)
    if header and header.group(2) not in _NON_DIALOGUE_IDS:
        return header.group(2)
    return None


def parse_tl_source(
    text: str, rel: str, seen_strings: set[str] | None = None
) -> list[TextUnit]:
    """TextUnits from one SDK-generated tl stub file."""
    seen = seen_strings if seen_strings is not None else set()
    units: list[TextUnit] = []
    block_id: str | None = None
    say_line: str | None = None
    for line in text.splitlines():
        if line.startswith("translate "):
            units += _dialogue_unit(block_id, say_line, rel)
            block_id = _block_id_from_header(line)
            say_line = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if block_id is not None:
            # ponytail: the say statement is the last quoted line of the block;
            # good enough until a stub shape shows up where it is not.
            if _STRING_RE.search(line):
                say_line = line
            continue
        old = _OLD_RE.match(line)
        if old and old.group(1) not in seen:
            seen.add(old.group(1))
            units.append(
                TextUnit(
                    id="strings_" + hashlib.md5(old.group(1).encode()).hexdigest()[:12],
                    source=_unescape(old.group(1)),
                    status=Status.UNTRANSLATED,
                    engine_ref={"file": rel, "kind": "strings", "old": old.group(1)},
                )
            )
    units += _dialogue_unit(block_id, say_line, rel)
    return units


def _dialogue_unit(block_id: str | None, say_line: str | None, rel: str) -> list[TextUnit]:
    """The dialogue block that just ended, or nothing when there was none or it is unreadable."""
    if block_id is None:
        return []
    if say_line is None:
        typer.secho(
            f"warning: skipped unparsable block {block_id} in {rel}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return []
    return [
        TextUnit(
            id=block_id,
            source=_unescape(_STRING_RE.findall(say_line)[-1]),
            status=Status.UNTRANSLATED,
            engine_ref={"file": rel, "kind": "dialogue"},
        )
    ]


def fill_tl_source(text: str, dialogue: dict[str, str], strings: dict[str, str]) -> str:
    """A filled copy of one tl stub: translations swapped into say / ``new`` lines."""
    lines = text.splitlines(keepends=True)
    replacements: dict[int, str] = {}
    block_id: str | None = None
    say_idx: int | None = None
    last_old: str | None = None

    def commit_block() -> None:
        if block_id is not None and say_idx is not None and block_id in dialogue:
            replacements[say_idx] = dialogue[block_id]

    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if line.startswith("translate "):
            commit_block()
            block_id = _block_id_from_header(line)
            say_idx = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if block_id is not None:
            if _STRING_RE.search(line):
                say_idx = i
        else:
            old = _OLD_RE.match(line)
            if old:
                last_old = old.group(1)
                continue
            new = _NEW_RE.match(line)
            if new and last_old in strings:
                replacements[i] = strings[last_old]
    commit_block()

    out: list[str] = []
    for i, raw in enumerate(lines):
        if i not in replacements:
            out.append(raw)
            continue
        line = raw.rstrip("\r\n")
        eol = raw[len(line) :]
        quoted = list(_STRING_RE.finditer(line))[-1]
        out.append(
            line[: quoted.start(1)] + _escape(replacements[i]) + line[quoted.end(1) :] + eol
        )
    return "".join(out)


_MISSING_SDK_MSG = (
    "The renpy engine needs the official Ren'Py SDK (free): "
    "https://www.renpy.org/latest.html\n"
    "Set [engine] sdk_path in project.toml to the unpacked SDK directory."
)


class RenpyAdapter(EngineAdapter):
    """Adapter for Ren'Py games via the SDK's official ``translate`` command."""

    name = "renpy"

    def __init__(self, sdk_path: str | Path = "", language: str = "") -> None:
        self.sdk_path = Path(sdk_path) if str(sdk_path) else None
        self.language = language

    def _sdk_exe(self) -> Path:
        if self.sdk_path is None:
            raise FileNotFoundError(_MISSING_SDK_MSG)
        exe = self.sdk_path / ("renpy.exe" if sys.platform == "win32" else "renpy.sh")
        if not exe.is_file():
            raise FileNotFoundError(
                f"Ren'Py SDK executable not found: {exe}\n{_MISSING_SDK_MSG}"
            )
        return exe

    def _tl_dir(self, game_path: Path) -> Path:
        return game_path / "game" / "tl" / self.language

    def _run_sdk_translate(self, game_path: Path) -> None:
        cmd = [str(self._sdk_exe()), str(game_path), "translate", self.language]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Ren'Py SDK translate failed ({' '.join(cmd)}):\n"
                f"{result.stderr or result.stdout}"
            )

    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        del sink, token
        self._run_sdk_translate(game_path)
        tl_dir = self._tl_dir(game_path)
        stubs = sorted(tl_dir.rglob("*.rpy")) if tl_dir.is_dir() else []
        if not stubs:
            raise RuntimeError(
                f"SDK translate produced no stub files in {tl_dir} — "
                f"is {game_path} a Ren'Py project directory (containing game/)?"
            )
        units: list[TextUnit] = []
        seen: set[str] = set()
        for path in stubs:
            rel = path.relative_to(tl_dir).as_posix()
            units.extend(parse_tl_source(path.read_text(encoding="utf-8"), rel, seen))
        return units

    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        del sink, token
        translated = [u for u in units if u.target]
        dialogue = {
            u.id: u.target for u in translated if u.engine_ref.get("kind") == "dialogue"
        }
        strings = {
            u.engine_ref["old"]: u.target
            for u in translated
            if u.engine_ref.get("kind") == "strings"
        }
        tl_dir = self._tl_dir(game_path)
        for path in sorted(tl_dir.rglob("*.rpy")):
            rel = path.relative_to(tl_dir)
            dest = out_path / "game" / "tl" / self.language / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(
                fill_tl_source(path.read_text(encoding="utf-8"), dialogue, strings),
                encoding="utf-8",
            )
