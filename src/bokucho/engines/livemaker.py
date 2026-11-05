"""LiveMaker engine adapter — wraps pylivemaker (reference implementation)."""

from __future__ import annotations

import io
import itertools
import logging
import re
import shutil
import struct
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path, PurePath, PureWindowsPath
from xml.etree import ElementTree

from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressLevel,
    ProgressSink,
)
from bokucho.engines.base import (
    EngineAdapter,
    NoPixelFont,
    archive_digest,
    holds_original,
    unpacked_stamp,
)
from bokucho.engines.cp932_proxy import MissingGlyphError, ProxyMap
from bokucho.engines.livemaker_runs import (
    Run,
    VariableMismatch,
    find_runs,
    render_run,
    run_source,
    splice_runs,
)
from bokucho.findings import Finding
from bokucho.model import Context, Status, TextUnit

try:
    import construct
    from livemaker.archive import LMArchive, LMCompressType
    from livemaker.exceptions import BadLsbError, LiveMakerException
    from livemaker.lpb import LMProject
    from livemaker.lpm import LMLivePrevMenu
    from livemaker.lsb import LMScript
    from livemaker.lsb.command import Calc, LabelReference
    from livemaker.lsb.core import (
        LiveParser,
        OpeData,
        OpeDataType,
        OpeFuncType,
        Param,
        ParamType,
    )
    from livemaker.lsb.menu import TextSelectionMenu
    from livemaker.lsb.translate import (
        TextBlockIdentifier,
        TextMenuIdentifier,
        make_identifier,
    )
    from livemaker.project import PylmProject

    # Imports pylivemaker itself, so it is only there when pylivemaker is.
    from bokucho.engines.livemaker_values import (
        Values,
        analyse,
        assembled,
        inconsistencies,
        mismatches,
        value_id,
        worded,
    )

    class _IndexedScript(LMScript):
        """Use command tags to select the existing binary schemas."""

        @classmethod
        @lru_cache(maxsize=1)
        def _struct(cls) -> construct.Struct:
            schema = LMScript._struct()
            commands = schema.commands.subcon.items.subcon
            selector = commands.subcon
            encoding = selector.subcons[0].type.subcon.subcon.encmapping
            cases = {
                encoding[variant.type.subcon.value]: variant for variant in selector.subcons
            }
            peek = construct.Peek(construct.Byte)

            def command_type(context: construct.Container) -> int | None:
                if context._building:
                    return encoding.get(context["items"][context._index]["type"])
                return peek.parse_stream(context._io)

            commands.subcon = construct.Select(
                construct.Switch(command_type, cases, default=selector), selector
            )
            return schema

    _IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    _IMPORT_ERROR = exc

_log = logging.getLogger(__name__)

FONT_SLOTS = ("MesNew", "Menu", "Caption", "Button", "EditNew")
CHOSEN = "選択値"
CHOICE_ID = "pylm:menu-text:"

#: How a table is loaded, and a row in it looked up by one column's cell and
#: read from another, as pylivemaker prints an expression or a text holds one.
_TABLE_LOAD = re.compile(r'DBLoadTsvFile\(\s*"([^"]*)"\s*\)')
_TABLE_KEY = re.compile(r'DBDirectGetStr\(\s*"[^"]*"\s*,\s*"([^"]*)"')
_TABLE_READ = re.compile(
    r'DBDirectGetStr\(\s*"[^"]*"\s*,\s*"[^"]*"\s*,.*?,\s*"([^"]*)"\s*,\s*"[^"]*"\s*\)'
)
#: Any other function that finds, sorts or writes rows by a column's cell.
_TABLE_OTHER = re.compile(
    r"\bDB(?:Locate|Find(?:First|Last|Next|Prior)|Sort|DirectGet(?:Int|Float|Bool)|DirectSet\w+)\("
)
_LITERAL = re.compile(r'"([^"]*)"')
#: What a table cell may end in for the text after it to start a new line.
_CELL_BREAK = "\\n"

PROJECT_SETTINGS = "live.lpb"
_STATUS_FONT = b"StatusFontName"


def _status_font_value(lpb: bytes) -> tuple[int, int]:
    """Where StatusFontName's value starts in ``lpb``, and how many bytes it takes."""
    marker = struct.pack("<I", len(_STATUS_FONT)) + _STATUS_FONT
    if lpb.count(marker) != 1:
        raise ValueError(f"{PROJECT_SETTINGS} holds no single StatusFontName setting.")
    start = lpb.index(marker) + len(marker) + 4
    if start > len(lpb):
        raise ValueError(f"{PROJECT_SETTINGS}'s StatusFontName runs past the end of the file.")
    (length,) = struct.unpack_from("<I", lpb, start - 4)
    if start + length > len(lpb):
        raise ValueError(f"{PROJECT_SETTINGS}'s StatusFontName runs past the end of the file.")
    return start, length


def status_font_name(lpb: bytes) -> bytes:
    """The CP932 bytes of the face the game starts StatusFontName from."""
    start, length = _status_font_value(lpb)
    return lpb[start : start + length]


def _status_font_literal(command) -> Param | None:
    """The face of a ``StatusFontName = "<face>"`` command, or None for any other."""
    if not isinstance(command, Calc):
        return None
    entries = command["Calc"].entries
    if len(entries) != 2 or entries[1].name != _STATUS_FONT.decode():
        return None
    operand = entries[0].operands[0]
    return operand if operand.type == ParamType.Str else None


def replace_status_font(lpb: bytes, family: str) -> bytes:
    """``lpb`` with StatusFontName's default set to ``family``; every other byte as it was."""
    start, length = _status_font_value(lpb)
    value = family.encode("cp932")
    if len(value) != length:
        raise ValueError(f"{family!r} is {len(value)} bytes; the default it replaces is {length}.")
    return lpb[:start] + value + lpb[start + length :]


_MISSING_DEP_MSG = (
    "The LiveMaker engine requires pylivemaker. Install it with:\n"
    "    pip install 'bokucho[livemaker]'"
)


_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()


_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{digit}" for digit in "123456789"}
    | {f"LPT{digit}" for digit in "123456789"}
)
_SUPERSCRIPT_DIGITS = str.maketrans("¹²³", "123")


def _names_a_device(part: str) -> bool:
    """Whether Windows would open a device rather than this file."""
    stem = part.split(".", 1)[0].rstrip(" .").upper().translate(_SUPERSCRIPT_DIGITS)
    return stem in _RESERVED_DEVICE_NAMES


def _validated_archive_parts(entry_path: PurePath) -> tuple[str, ...]:
    """Return safe relative archive components without filesystem access."""
    parts = entry_path.parts
    if entry_path.is_absolute() or entry_path.anchor:
        raise ValueError(f"Unsafe LiveMaker archive entry path: {entry_path}")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe LiveMaker archive entry path: {entry_path}")
    if any(_names_a_device(part) for part in parts):
        raise ValueError(
            f"LiveMaker archive entry names a Windows device: {entry_path}"
        )
    return parts


def _resolved_cache_path(workspace: Path, candidate: Path) -> Path:
    """Resolve a cache path and require it to remain within its workspace."""
    resolved = candidate.resolve()
    if resolved == workspace or not resolved.is_relative_to(workspace):
        raise ValueError(f"LiveMaker cache path escapes its workspace: {candidate}")
    return resolved


def _scene_name(name: object) -> str | None:
    """A block's scene as text, whatever LiveMaker wrote it as."""
    if name is None or isinstance(name, str):
        return name or None
    return str(name) or None


def _operands(command) -> list:
    """Every operand of every expression ``command`` holds."""
    return [
        operand
        for value in command.args.values()
        for entry in getattr(value, "entries", ())
        for operand in entry.operands
        if operand is not None
    ]


def _expressions(command):
    """Every expression ``command`` holds, a Call's parameters included."""
    for value in command.args.values():
        if getattr(value, "entries", None) is not None:
            yield value
        yield from getattr(value, "parsers", None) or ()


def _offered(command):
    """The string operand of ``AddArray(_tmp, "<words>")``, or None for any other command."""
    entries = getattr(command.args.get("Calc"), "entries", ())
    if len(entries) != 3 or entries[1].func != OpeFuncType.AddArray:
        return None
    menu = entries[1].operands[0]
    words = entries[0].operands[0] if entries[0].operands else None
    if menu is None or menu.value != "_tmp" or words is None or words.type != ParamType.Str:
        return None
    return words


def _buttons(path: Path) -> list[str]:
    """The names of an image menu's buttons; the one clicked is the pick."""
    data = path.read_bytes()
    if data.startswith(b"LivePrevMenu"):
        menu = LMLivePrevMenu.from_file(io.BytesIO(data))
        return [button.get("name") for button in menu["buttons"]]
    menu = ElementTree.fromstring(data.decode("cp932"))
    return [name.text or "" for name in menu.iterfind("Button/Item/Name")]


def _choice_literal(lsb, choice):
    """The string operand a choice's words are, or None for words the game computes."""
    operand = _offered(lsb.commands[choice.text_index])
    return operand if operand is not None and operand.value == choice.text else None


def _table_calls(scripts: dict) -> list[str]:
    """Every expression, or text, of ``scripts`` that loads or reads a table."""
    calls = []
    for rel, lsb in scripts.items():
        for command in lsb.commands:
            for expression in _expressions(command):
                if any(
                    isinstance(entry.func, OpeFuncType) and entry.func.name.startswith("DB")
                    for entry in expression.entries
                ):
                    calls.append(str(expression))
        try:
            scenarios = lsb.text_scenarios(run_order=False)
        except (LiveMakerException, AttributeError, KeyError, IndexError, ValueError) as exc:
            _log.warning("Skipped the table reads in the text of %s: %s", rel, exc)
            continue
        # The backlog names who speaks with a lookup in its text.
        calls.extend(
            glyph.var_name
            for _, _, scenario in scenarios
            for glyph in scenario.body
            if "DBDirectGetStr" in (getattr(glyph, "var_name", None) or "")
        )
    return calls


def _found(root: Path, name: str) -> Path | None:
    """The file a script names, backslashes and all, as ``root`` spells it: case is ignored."""
    path = root
    for part in PureWindowsPath(name).parts:
        if not path.is_dir():
            return None
        path = next(
            (child for child in path.iterdir() if child.name.casefold() == part.casefold()),
            None,
        )
        if path is None:
            return None
    return path if path.is_file() else None


def _table(path: Path) -> list[list[bytes]]:
    """A TSV table's lines of raw CP932 cells, names first: untouched cells keep their bytes."""
    return [line.split(b"\t") for line in path.read_bytes().split(b"\r\n")]


def _face_literals(entries) -> list:
    """The string operands of a font-name expression that name the face."""
    # ponytail: a string reaching a function through two temporaries is still
    # renamed; no game seen nests them, follow the chain if one does.
    passed = {
        operand.value
        for entry in entries
        if entry.type == OpeDataType.Func
        for operand in entry.operands
        if operand is not None and operand.type == ParamType.Var
    }
    return [
        operand
        for entry in entries
        if entry.type != OpeDataType.Func and entry.name not in passed
        for operand in entry.operands
        if operand is not None and operand.type == ParamType.Str
    ]


def _translated_values(units: list[TextUnit]) -> dict[str, str]:
    """Each translated value's word and its translation."""
    return {unit.source: unit.target for unit in units if unit.kind == "value" and unit.target}


def _merged_unit(rel, id_, block, ordinal: int, run: Run, body, block_ids, decode) -> TextUnit:
    """One unit for a sentence cut by variables, the variables inline."""
    name = _scene_name(id_.name)
    unit_id = f"pylm:text:{id_.filename}:{id_.line_no}:{run.first_block}-{run.last_block}"
    parts = [
        {"block": block_ids[(id_.line_no, part["block"])]} if "block" in part else dict(part)
        for part in run.parts
    ]
    return TextUnit(
        id=unit_id,
        source=run_source(body, run, decode),
        status=Status.UNTRANSLATED,
        context=Context(scene=name),
        engine_ref={
            "lsb": rel,
            "block_id": unit_id,
            "name": name,
            "label": block.name_label,
            "line_no": id_.line_no,
            "run": ordinal,
            "parts": parts,
        },
    )


def _units_by_script(units: list[TextUnit], root: Path) -> dict[str, list[TextUnit]]:
    """The units that came from each script, refusing a script no longer in the game."""
    by_lsb: dict[str, list[TextUnit]] = defaultdict(list)
    for unit in units:
        rel = unit.engine_ref.get("lsb")
        if rel:
            by_lsb[rel].append(unit)
    for rel in by_lsb:
        if not (root / rel).is_file():
            raise FileNotFoundError(f"LSB file not found for injection: {root / rel}")
    return by_lsb


class LiveMakerAdapter(EngineAdapter):
    """Engine adapter for LiveMaker / LiveNovel games, backed by pylivemaker."""

    name = "livemaker"

    def __init__(
        self, proxy_map: Path | None = None, workspace: Path | None = None
    ) -> None:
        if _IMPORT_ERROR is not None:
            raise ImportError(_MISSING_DEP_MSG) from _IMPORT_ERROR
        self.proxy = (
            ProxyMap.load(proxy_map) if proxy_map and Path(proxy_map).is_file() else None
        )
        self.workspace = Path(workspace) if workspace else None

    def extract(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> list[TextUnit]:
        root = self._game_root(game_path, sink, token)
        units: list[TextUnit] = []
        # ponytail: every script stays in memory until the values are followed;
        # analyse per script and keep only the graph if a game outgrows that.
        scripts: dict[str, LMScript] = {}
        paths = sorted(root.rglob("*.lsb"))
        for index, lsb_path in enumerate(paths, start=1):
            token.raise_if_cancelled()
            rel = lsb_path.relative_to(root).as_posix()
            try:
                lsb = self._open(lsb_path)
                scripts[rel] = lsb
                blocks = lsb.get_text_blocks()
                runs = {
                    line_no: (scenario.body, find_runs(scenario.body, scenario.get_text_blocks()))
                    for line_no, _, scenario in lsb.text_scenarios(run_order=False)
                }
            except (LiveMakerException, OSError) as exc:
                _log.warning("Skipped %s: %s", rel, exc)
                sink.publish(
                    ProgressEvent(
                        "engine.extract",
                        index,
                        len(paths),
                        "engine.extract.file.skipped",
                        level=ProgressLevel.WARNING,
                        detail=rel,
                    )
                )
                continue
            units.extend(self._block_units(rel, blocks, runs))
            units.extend(self._menu_units(lsb, rel, self._decode))
            _log.info(
                "Extracted %d line(s) from %s (%d/%d)",
                len(blocks),
                rel,
                index,
                len(paths),
            )
            sink.publish(
                ProgressEvent(
                    "engine.extract",
                    index,
                    len(paths),
                    "engine.extract.file.completed",
                    detail=rel,
                )
            )
        token.raise_if_cancelled()
        units.extend(self._value_units(scripts))
        units.extend(self._table_units(root, scripts))
        return units

    def _block_units(self, rel: str, blocks: list, runs: dict) -> list[TextUnit]:
        """One unit per text block, or per run of blocks its scenario draws as one line."""
        block_ids = {(id_.line_no, id_.block_index): str(id_) for id_, _ in blocks}
        units = []
        for id_, block in blocks:
            body, line_runs = runs.get(id_.line_no, ((), []))
            found = next(
                (
                    (ordinal, run)
                    for ordinal, run in enumerate(line_runs)
                    if run.first_block <= id_.block_index <= run.last_block
                ),
                None,
            )
            if found is None:
                name = _scene_name(id_.name)
                units.append(
                    TextUnit(
                        id=str(id_),
                        source=self._decode(block.text),
                        status=Status.UNTRANSLATED,
                        context=Context(scene=name),
                        engine_ref={
                            "lsb": rel,
                            "block_id": str(id_),
                            "name": name,
                            "label": block.name_label,
                        },
                    )
                )
                continue
            ordinal, run = found
            if id_.block_index == run.first_block:
                units.append(
                    _merged_unit(rel, id_, block, ordinal, run, body, block_ids, self._decode)
                )
        return units

    def _table_units(self, root: Path, scripts: dict[str, LMScript]) -> list[TextUnit]:
        """One unit for each worded cell the game shows from a table it loads.

        A column is shown when some lookup reads it, and stays as it is when
        some lookup finds rows by it.
        """
        # ponytail: columns are matched by name across every table, not table by
        # table; follow the table name from DBCreateTable if two tables clash.
        calls = _table_calls(scripts)
        keys = {key for call in calls for key in _TABLE_KEY.findall(call)}
        keys |= {
            key for call in calls if _TABLE_OTHER.search(call) for key in _LITERAL.findall(call)
        }
        shown = {column for call in calls for column in _TABLE_READ.findall(call)} - keys
        tables = {
            found.relative_to(root).as_posix(): found
            for call in calls
            for name in _TABLE_LOAD.findall(call)
            if (found := _found(root, name)) is not None
        }
        units = []
        for rel, path in sorted(tables.items()):
            try:
                columns, *rows = [[cell.decode("cp932") for cell in line] for line in _table(path)]
            except UnicodeDecodeError as exc:
                _log.warning("Skipped the table %s: it is not CP932 text (%s).", rel, exc)
                continue
            for row, cells in enumerate(rows, start=1):
                for column, cell in zip(columns, cells, strict=False):
                    text = cell.removesuffix(_CELL_BREAK)
                    if column in shown and worded(text):
                        units.append(
                            TextUnit(
                                id=f"pylm:tsv:{rel}:{row}:{column}",
                                source=self._decode(text),
                                status=Status.UNTRANSLATED,
                                engine_ref={"file": rel, "row": row, "column": column},
                            )
                        )
        return units

    def _value_units(self, scripts: dict[str, LMScript]) -> list[TextUnit]:
        """One unit for each word the game shows from a variable, game-wide."""
        values = analyse(scripts, self._decode)
        for word, names in sorted(values.excluded().items()):
            _log.info(
                "Left %r untranslated: %s is also used as a key", word, ", ".join(sorted(names))
            )
        return [
            TextUnit(
                id=value_id(word),
                source=word,
                status=Status.UNTRANSLATED,
                kind="value",
                context=Context(tags=sorted(names)),
                engine_ref={"file": "values", "vars": sorted(names)},
            )
            for word, names in sorted(values.words().items())
        ]

    @staticmethod
    def _menu_units(lsb: LMScript, rel: str, decode) -> list[TextUnit]:
        """One unit for each worded choice of each text selection menu in ``lsb``."""
        try:
            menus = lsb.get_menus(run_order=False)
        except (LiveMakerException, AttributeError, KeyError, IndexError, ValueError) as exc:
            _log.warning("Skipped the menus of %s: %s", rel, exc)
            return []
        return [
            TextUnit(
                id=str(id_),
                source=decode(choice.text),
                status=Status.UNTRANSLATED,
                engine_ref={"lsb": rel, "block_id": str(id_)},
            )
            for line_no, menu in menus
            if isinstance(menu, TextSelectionMenu)
            for index, choice in enumerate(menu.choices)
            if _choice_literal(lsb, choice) is not None
            for id_ in [TextMenuIdentifier(lsb.call_name, line_no, choice_index=index)]
        ]

    def inject(
        self,
        units: list[TextUnit],
        game_path: Path,
        out_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        root = self._game_root(game_path, sink, token)
        out_path = Path(out_path)

        by_lsb = _units_by_script(units, root)
        paths = sorted(root.rglob("*.lsb"))
        translated = _translated_values(units)
        opened: dict[Path, LMScript] = {}
        values = None
        if translated:
            opened, values = self._analyse(root, paths, token)
            self._check_values(values, translated)

        def load(path: Path) -> LMScript:
            return opened[path] if path in opened else self._open(path)

        shown = {
            word: {word, translated[word]}
            for word in (values.offered() if values else ())
            if word in translated
        }
        becomes, chosen = self._menu_words(units, root, paths, token, shown, load)
        owned = self._write_values(values, translated, chosen) if values else set()
        self._write_tables(units, root, out_path)
        for index, lsb_path in enumerate(paths, start=1):
            token.raise_if_cancelled()
            rel = lsb_path.relative_to(root).as_posix()
            lsb = load(lsb_path)
            written, changed = self._rewrite_script(
                lsb, lsb_path, by_lsb.get(rel, []), becomes, chosen, owned
            )
            dst = out_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if changed or translated:
                dst.write_bytes(self._serialize_validated(lsb))
            elif not dst.exists() or not dst.samefile(lsb_path):
                shutil.copy2(lsb_path, dst)
            _log.info(
                "Wrote %d line(s) back to %s (%d/%d)",
                written,
                rel,
                index,
                len(paths),
            )
            sink.publish(
                ProgressEvent(
                    "engine.inject",
                    index,
                    len(paths),
                    "engine.inject.file.completed",
                    detail=rel,
                )
            )

    def _rewrite_script(
        self,
        lsb: LMScript,
        lsb_path: Path,
        lsb_units: list[TextUnit],
        becomes: dict,
        chosen: dict,
        owned: set,
    ) -> tuple[int, bool]:
        """Write the translations into ``lsb``: how many lines, and whether it changed."""
        merged = [u for u in lsb_units if "parts" in u.engine_ref and u.target]
        scenarios = {}
        if merged:
            scenarios = {
                line_no: scenario
                for line_no, _, scenario in lsb.text_scenarios(run_order=False)
            }
            self._check_runs(merged, scenarios)
        text_objects = []
        choices = []
        for unit in lsb_units:
            if not unit.target or "parts" in unit.engine_ref:
                continue
            id_ = make_identifier(unit.engine_ref["block_id"])
            if isinstance(id_, TextMenuIdentifier):
                choices.append((id_, unit))
            elif isinstance(id_, TextBlockIdentifier) and id_.filename == lsb_path.name:
                text_objects.append((id_, self._encode(unit.target, unit.id)))
        if text_objects:
            lsb.replace_text(text_objects)
        if merged:
            self._write_runs(merged, scenarios)
        if choices or becomes:
            self._write_choices(lsb, choices, becomes, chosen, owned)
        written = len(text_objects) + len(merged) + len(choices)
        return written, bool(text_objects or merged or choices or becomes)

    def _write_tables(self, units: list[TextUnit], root: Path, out_path: Path) -> None:
        """Write each translated cell into its table, the rest of the file as it was."""
        by_table: dict[str, list[TextUnit]] = defaultdict(list)
        for unit in units:
            if "column" in unit.engine_ref and unit.target:
                by_table[unit.engine_ref["file"]].append(unit)
        for rel, cells in by_table.items():
            # The name comes from the project's strings, so it may only name a
            # file of the game.
            path = (root / rel).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise FileNotFoundError(f"Table not found for injection: {root / rel}")
            header, *rows = _table(path)
            columns = [cell.decode("cp932", errors="replace") for cell in header]
            for unit in cells:
                row, column = unit.engine_ref["row"], unit.engine_ref["column"]
                at = columns.index(column) if column in columns else None
                cell = (
                    rows[row - 1][at].decode("cp932", errors="replace")
                    if at is not None and 0 < row <= len(rows) and at < len(rows[row - 1])
                    else None
                )
                text = cell.removesuffix(_CELL_BREAK) if cell is not None else None
                if text is None or self._decode(text) != unit.source:
                    raise ValueError(
                        f"{unit.id}: the game's table changed since extraction; extract again"
                    )
                if any(ch in unit.target for ch in "\t\r\n"):
                    raise ValueError(f"{unit.id}: a table cell cannot hold a tab or a line break")
                written = self._encode(unit.target, unit.id) + cell[len(text) :]
                rows[row - 1][at] = written.encode("cp932")
            dst = out_path / path.relative_to(root.resolve())
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"\r\n".join(b"\t".join(line) for line in (header, *rows)))

    def _decode(self, text: str) -> str:
        return self.proxy.decode(text) if self.proxy else text

    def _encode(self, text: str, where: str) -> str:
        """``text`` as the game stores it: donors for what CP932 lacks."""
        if not self.proxy:
            return text
        try:
            return self.proxy.encode(text)
        except MissingGlyphError as exc:
            raise ValueError(
                f"{where}: character {exc.args[0]!r} has no proxy mapping. "
                f"Run `bokucho patch-runtime` to rebuild the proxy map."
            ) from exc

    def check_values(
        self,
        units: list[TextUnit],
        game_path: Path,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        """What :meth:`inject` refuses about translated values, writing nothing."""
        translated = _translated_values(units)
        if translated:
            self._check_values(self.analyse_values(game_path, token), translated)

    def analyse_values(
        self, game_path: Path, token: CancellationToken = _NEVER_CANCELLED
    ) -> Values:
        """The values followed through every script; they change only with the game."""
        root = self._game_root(game_path, token=token)
        return self._analyse(root, sorted(root.rglob("*.lsb")), token)[1]

    @staticmethod
    def value_problems(values: Values, units: list[TextUnit]) -> list[tuple[str, Finding]]:
        """(unit id, finding) for each value the game derives that no longer matches."""
        translated = _translated_values(units)
        return [
            (value_id(whole), finding)
            for finding, whole, _ in mismatches(values, lambda word: translated.get(word, word))
        ]

    @staticmethod
    def assemble_values(values: Values, units: list[TextUnit]) -> dict[str, str]:
        """Unit id -> target for the machine drafts the game's derived values need."""
        translated = _translated_values(units)
        drafts = {
            unit.source for unit in units if unit.kind == "value" and unit.status is Status.MT
        }
        fixes = assembled(values, lambda word: translated.get(word, word), drafts.__contains__)
        for word, target in fixes.items():
            _log.info("Assembled %r from its parts: %r, was %r", word, target, translated[word])
        return {value_id(word): target for word, target in fixes.items()}

    def _analyse(
        self, root: Path, paths: list[Path], token: CancellationToken
    ) -> tuple[dict[Path, LMScript], Values]:
        """Every script read, and the values followed through all of them."""
        opened: dict[Path, LMScript] = {}
        for path in paths:
            token.raise_if_cancelled()
            opened[path] = self._open(path)
        values = analyse(
            {path.relative_to(root).as_posix(): lsb for path, lsb in opened.items()},
            self._decode,
        )
        return opened, values

    @staticmethod
    def _check_values(values: Values, translated: dict[str, str]) -> None:
        """Refuse what cannot be written: a value no longer shown, or derived inconsistently."""
        stale = sorted(set(translated) - set(values.words()))
        if stale:
            raise ValueError(
                f"{value_id(stale[0])}: the game no longer shows {stale[0]!r}; extract again"
            )
        problems = inconsistencies(values, lambda word: translated.get(word, word))
        if problems:
            raise ValueError(
                "Values the game derives from one another would no longer match; "
                "translate them alike:\n" + "\n".join(problems[:20])
            )

    def _write_values(
        self, values: Values, translated: dict[str, str], chosen: set[str]
    ) -> set[int]:
        """Write each value's translation where it is assigned, and every check on it."""
        owned = {id(site.operand) for site in values.sites}
        for site in values.sites:
            if (
                site.word in translated
                and values.translatable(site)
                and id(site.operand) not in values.fixed
            ):
                site.operand.value = self._encode(translated[site.word], value_id(site.word))
        owned |= self._write_compares(values, translated, chosen)
        owned |= self._write_needles(values, translated)
        for replace in values.replaces:
            if not values.reaches(replace.subject):
                continue
            for word, operand in replace.find.words + replace.replacement.words:
                target = translated.get(word, word)
                if target != word:
                    operand.value = self._encode(target, value_id(word))
                    owned.add(id(operand))
        return owned

    def _write_compares(
        self, values: Values, translated: dict[str, str], chosen: set[str]
    ) -> set[int]:
        """Have each comparison with a translated value hold for its translation too."""
        owned: set[int] = set()
        for compare in values.compares:
            word = compare.word
            target = translated.get(word, word)
            if (
                target == word
                or compare.places.isdisjoint(values.reached)
                or any(place[1] in chosen for place in compare.places)
            ):
                continue
            where = f"{compare.script}:{compare.command.LineNo}"
            added = self._either(compare.command, compare.operand, sorted({word, target}), where)
            if added is None:
                raise ValueError(
                    f"{where} checks a value against {word!r} ({value_id(word)}) in a way "
                    f"Bokucho cannot make take {target!r} too; leave it untranslated"
                )
            owned.update(map(id, [compare.operand, *added]))
        return owned

    def _write_needles(self, values: Values, translated: dict[str, str]) -> set[int]:
        """Have each search for a translated value find its translation too."""
        owned: set[int] = set()
        for needle in values.needles:
            if not values.reaches(needle.haystack) or needle.needle.places:
                continue
            if len(needle.needle.words) != 1:
                continue
            ((word, operand),) = needle.needle.words
            target = translated.get(word, word)
            if target == word:
                continue
            where = f"{needle.script}:{needle.command.LineNo}"
            added = self._either_position(needle.command, operand, sorted({word, target}), where)
            if added is None:
                raise ValueError(
                    f"{where} searches a value for {word!r} ({value_id(word)}) in a way "
                    f"Bokucho cannot make find {target!r} too; leave it untranslated"
                )
            owned.update(map(id, [operand, *added]))
        return owned

    def _menu_words(
        self,
        units: list[TextUnit],
        root: Path,
        paths: list[Path],
        token: CancellationToken,
        shown: dict[str, set[str]],
        load: Callable[[Path], LMScript],
    ) -> tuple[dict[str, set[str]], set[str]]:
        """What each word the menus offer is shown as, game-wide, and what holds the pick."""
        becomes: dict[str, set[str]] = defaultdict(set)
        for word, forms in shown.items():
            becomes[word] |= forms
        written: Counter[str] = Counter()
        for unit in units:
            if unit.engine_ref.get("block_id", "").startswith(CHOICE_ID):
                becomes[unit.source].add(unit.target or unit.source)
                written[unit.source] += 1
        if all(words == {word} for word, words in becomes.items()):
            return {}, set()
        offered: Counter[str] = Counter()
        copies: dict[str, set[str]] = defaultdict(set)
        for path in paths:
            token.raise_if_cancelled()
            for command in load(path).commands:
                for value in command.args.values():
                    self._scan_menu_value(getattr(value, "entries", ()), offered, copies)
        for path in sorted(root.rglob("*.lpm")):
            try:
                offered.update(self._decode(name) for name in _buttons(path))
            except (LiveMakerException, ElementTree.ParseError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"Cannot read the image menu {path.relative_to(root).as_posix()} "
                    f"({exc}): the name of the button clicked is the pick, and without "
                    "it no check of the pick can be told to follow a translation"
                ) from exc
        for word, words in becomes.items():
            if offered[word] > written[word]:
                words.add(word)
        chosen, pending = {CHOSEN}, [CHOSEN]
        while pending:
            for name in copies.get(pending.pop(), ()):
                if name not in chosen:
                    chosen.add(name)
                    pending.append(name)
        return becomes, chosen

    def _scan_menu_value(
        self, entries, offered: Counter[str], copies: dict[str, set[str]]
    ) -> None:
        """Count the words an expression puts in a menu, and note each variable it copies."""
        if any(
            entry.name == "_tmp"
            or any(
                op is not None and op.type == ParamType.Var and op.value == "_tmp"
                for op in entry.operands
            )
            for entry in entries
        ):
            offered.update(
                self._decode(op.value)
                for entry in entries
                for op in entry.operands
                if op is not None and op.type == ParamType.Str
            )
        for entry in entries:
            operand = entry.operands[0] if len(entry.operands) == 1 else None
            if (
                entry.type == OpeDataType.To
                and not entry.name.startswith("____")
                and operand is not None
                and operand.type == ParamType.Var
            ):
                copies[operand.value].add(entry.name)

    def _write_choices(
        self,
        lsb: LMScript,
        choices: list,
        becomes: dict[str, set[str]],
        chosen: set[str],
        owned: set[int],
    ) -> None:
        """Write translated menu choices, and every check of the pick on their words."""

        def moves(operand) -> bool:
            if operand.type != ParamType.Str or id(operand) in owned:
                return False
            word = self._decode(operand.value)
            return becomes.get(word, {word}) != {word}

        checks = []
        for index, command in enumerate(lsb.commands):
            operands = _operands(command)
            if any(op.type == ParamType.Var and op.value in chosen for op in operands):
                checks += [(index, command, op) for op in operands if moves(op)]
        if not choices and not checks:
            return
        try:
            menus = dict(lsb.get_menus(run_order=False))
        except (LiveMakerException, AttributeError, KeyError, IndexError, ValueError):
            if choices:
                raise
            menus = {}
        self._write_menu_choices(lsb, menus, choices)
        in_menus = {
            index
            for menu in menus.values()
            for choice in menu.choices
            for index in (choice.text_index, choice.target_index)
        }
        for index, command, operand in checks:
            if index not in in_menus:
                self._write_pick_check(lsb, command, operand, becomes, chosen)

    def _write_menu_choices(self, lsb: LMScript, menus: dict, choices: list) -> None:
        """Put each translated choice in its menu; no menu may offer one word twice."""
        shown = {line_no: [c.text for c in menu.choices] for line_no, menu in menus.items()}
        for id_, unit in choices:
            menu = menus.get(id_.line_no)
            in_menu = menu is not None and id_.choice_index < len(menu.choices)
            choice = menu.choices[id_.choice_index] if in_menu else None
            literal = _choice_literal(lsb, choice) if choice else None
            if literal is None or self._decode(choice.text) != unit.source:
                raise ValueError(
                    f"{unit.id}: the game's menu changed since extraction; extract again"
                )
            target = self._encode(unit.target, unit.id)
            shown[id_.line_no][id_.choice_index] = target
            literal.value = target
            for operand in _operands(lsb.commands[choice.target_index]):
                if operand.type == ParamType.Str and operand.value == choice.text:
                    operand.value = target
        for line_no in sorted({id_.line_no for id_, _ in choices}):
            words = shown[line_no]
            twice = sorted({self._decode(word) for word in words if words.count(word) > 1})
            if twice:
                raise ValueError(
                    f"{lsb.call_name}:{line_no}: a menu would offer {', '.join(twice)} "
                    "more than once; give its choices different translations"
                )

    def _write_pick_check(
        self, lsb: LMScript, command, operand, becomes: dict[str, set[str]], chosen: set[str]
    ) -> None:
        """Have a check of the pick on a word hold for every form the menus show it as."""

        def picked(op) -> bool:
            return op.type == ParamType.Var and op.value in chosen

        where = f"{lsb.call_name}:{command.LineNo}"
        word = self._decode(operand.value)
        words = becomes[word]
        if len(words) == 1:
            operand.value = self._encode(next(iter(words)), where)
        elif self._either(command, operand, sorted(words), where, picked) is None:
            raise ValueError(
                f"{where} checks for {word!r}, which the game's menus show as "
                f"{', '.join(sorted(words))}, other than by comparing the pick with "
                "it: give every menu offering it the same translation, or leave "
                "them all untranslated"
            )

    def _either(self, command, operand, forms: list[str], where: str, of=None) -> list | None:
        """Have the comparison with ``operand`` hold for each of ``forms``."""
        found = self._word_user(command, operand)
        if found is None:
            return None
        entries, compare, is_word = found
        if compare.type not in (OpeDataType.Equal, OpeDataType.NEqual) or (
            of is not None and not any(op is not None and of(op) for op in compare.operands)
        ):
            return None
        joint = OpeDataType.Or if compare.type == OpeDataType.Equal else OpeDataType.And
        return self._fan_out(
            entries,
            compare,
            is_word,
            operand,
            forms,
            where,
            lambda name, sides: OpeData(joint, name, operands=sides),
        )

    def _either_position(self, command, operand, forms: list[str], where: str) -> list | None:
        """JPos("剣", 装備名[0]) becomes Max(JPos("剣", 装備名[0]), JPos("劍", 装備名[0]))."""
        found = self._word_user(command, operand)
        if found is None:
            return None
        entries, search, is_word = found
        if search.func != OpeFuncType.JPos or not is_word(search.operands[0]):
            return None
        return self._fan_out(
            entries,
            search,
            is_word,
            operand,
            forms,
            where,
            lambda name, sides: OpeData(OpeDataType.Func, name, OpeFuncType.Max, sides),
        )

    @staticmethod
    def _word_user(command, operand):
        """The entries holding ``operand``, the one entry using it, and a test for its uses."""
        found = next(
            (
                (parser.entries, entry)
                for parser in _expressions(command)
                for entry in parser.entries
                if any(op is operand for op in entry.operands)
            ),
            None,
        )
        if found is None:
            return None
        entries, holder = found
        temp = holder.name if holder.type == OpeDataType.To else None

        def is_word(op) -> bool:
            return op is operand or (
                op is not None and op.type == ParamType.Var and op.value == temp
            )

        users = [e for e in entries if e is not holder or temp is None]
        users = [e for e in users if any(is_word(op) for op in e.operands)]
        if len(users) != 1:
            return None
        return entries, users[0], is_word

    def _fan_out(self, entries, user, is_word, operand, forms, where, joint) -> list:
        """Repeat ``user`` once per form after the first, its results joined by ``joint``."""
        taken = {e.name for e in entries}
        fresh = (n for n in (f"____{i}" for i in itertools.count()) if n not in taken)
        operand.value = self._encode(forms[0], where)
        result, user.name = user.name, next(fresh)
        added, words, so_far = [], [], user.name
        for count, form in enumerate(forms[1:], start=2):
            text, again = next(fresh), next(fresh)
            word = Param(self._encode(form, where), ParamType.Str)
            words.append(word)
            added.append(OpeData(OpeDataType.To, text, operands=[word]))
            sides = [
                Param(text, ParamType.Var) if is_word(op) else Param(op.value, op.type)
                for op in user.operands
            ]
            added.append(OpeData(user.type, again, user.func, sides))
            name = result if count == len(forms) else next(fresh)
            added.append(joint(name, [Param(so_far, ParamType.Var), Param(again, ParamType.Var)]))
            so_far = name
        at = next(i for i, e in enumerate(entries) if e is user) + 1
        entries[at:at] = added
        return words

    def _check_runs(self, units: list[TextUnit], scenarios: dict) -> None:
        """Stop unless each sentence is still the one it was extracted as."""
        for unit in units:
            scenario = scenarios.get(unit.engine_ref["line_no"])
            runs = find_runs(scenario.body, scenario.get_text_blocks()) if scenario else []
            ordinal = unit.engine_ref["run"]
            if ordinal >= len(runs) or (
                run_source(scenario.body, runs[ordinal], self._decode) != unit.source
            ):
                raise ValueError(
                    f"{unit.id}: the game's line changed since extraction; extract again"
                )

    def _write_runs(self, units: list[TextUnit], scenarios: dict) -> None:
        """Write each translated sentence over its run, variables and all."""
        by_line: dict[int, list[TextUnit]] = defaultdict(list)
        for unit in units:
            by_line[unit.engine_ref["line_no"]].append(unit)
        encode = self.proxy.encode if self.proxy else str
        for line_no, line_units in by_line.items():
            scenario = scenarios[line_no]
            runs = find_runs(scenario.body, scenario.get_text_blocks())
            edits = []
            for unit in line_units:
                run = runs[unit.engine_ref["run"]]
                try:
                    edits.append((run, render_run(scenario.body, run, unit.target, encode)))
                except MissingGlyphError as exc:
                    raise ValueError(
                        f"{unit.id}: character {exc.args[0]!r} has no proxy mapping. "
                        f"Run `bokucho patch-runtime` to rebuild the proxy map."
                    ) from exc
                except (VariableMismatch, LiveMakerException) as exc:
                    raise ValueError(f"{unit.id}: {exc}") from exc
            scenario.replace_body(splice_runs(scenario.body, edits))

    def pixel_font(self, game_path: Path) -> Path:
        """The game's embedded ``.lft``, which it rasterises text from itself."""
        root = self._game_root(game_path)
        fonts = sorted(root.rglob("*.lft"))
        if not fonts:
            raise NoPixelFont(
                f"No .lft pixel font in {root}. This game draws its text with a "
                "system face, so the proxied characters cannot be drawn into it."
            )
        return fonts[0]

    def project_settings(self, game_path: Path) -> bytes:
        """The game's ``live.lpb``, where the defaults of its status variables live."""
        return (self._game_root(game_path) / PROJECT_SETTINGS).read_bytes()

    def retarget_fonts(
        self,
        overlay: Path,
        face: str,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> tuple[int, int]:
        """Point every literal face name in ``overlay`` at ``face``."""
        paths = sorted(overlay.rglob("*.lsb"))
        slots = 0
        for index, path in enumerate(paths, start=1):
            token.raise_if_cancelled()
            script = self._open(path)
            changed = False
            for command in script.commands:
                if getattr(command.type, "name", "") not in FONT_SLOTS:
                    continue
                if "PR_FONTNAME" not in command.args:
                    continue
                for operand in _face_literals(command["PR_FONTNAME"].entries):
                    if operand.value != face:
                        operand.value = face
                        changed = True
                    slots += 1
            if changed:
                path.write_bytes(self._serialize_validated(script))
            sink.publish(
                ProgressEvent(
                    "engine.font",
                    index,
                    len(paths),
                    "engine.font.script.completed",
                    detail=path.name,
                )
            )
        return len(paths), slots

    def start_with_face(
        self,
        overlay: Path,
        settings: bytes,
        face: str,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> None:
        """Have the game's start script set ``StatusFontName`` to ``face`` first."""
        start = PureWindowsPath(LMProject.from_file(io.BytesIO(settings)).init_lsb)
        path = overlay.joinpath(*start.parts)
        script = self._open(path)
        first = script.commands[0]
        literal = _status_font_literal(first)
        if literal is not None:
            literal.value = face
            path.write_bytes(self._serialize_validated(script))
            return
        line = first.LineNo
        for command in script.commands:
            command.LineNo += 1
        script.commands.insert(0, _status_font_assignment(face, line))
        page = start.with_suffix("")
        paths = sorted(overlay.rglob("*.lsb"))
        moved: dict[Path, LMScript] = {path: script}
        for index, other in enumerate(paths, start=1):
            token.raise_if_cancelled()
            lsb = script if other == path else self._open(other)
            if _shift_labels(lsb, page, line):
                moved[other] = lsb
            sink.publish(
                ProgressEvent(
                    "engine.font",
                    index,
                    len(paths),
                    "engine.font.script.completed",
                    detail=other.name,
                )
            )
        for other, lsb in moved.items():
            other.write_bytes(self._serialize_validated(lsb))

    def repack(
        self,
        game_path: Path,
        overlay: Path,
        out_dir: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> Path:
        """Rebuild the game archive with every overlay file that replaces one."""
        archive_path = Path(game_path)
        if not archive_path.is_file():
            raise FileNotFoundError(
                f"Repacking needs the packed game, not an unpacked directory: {archive_path}"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        destination = out_dir / archive_path.name
        with (
            LMArchive(archive_path) as original,
            tempfile.TemporaryDirectory(dir=out_dir) as staging_dir,
        ):
            staging = Path(staging_dir) / archive_path.name
            replacements = _overlay_replacements(original.infolist(), Path(overlay))
            stub = None
            if original.is_exe:
                stub = Path(staging_dir) / "stub.bin"
                stub.write_bytes(original.read_exe())
            with LMArchive(
                name=staging,
                mode="w",
                version=original.version,
                exe=str(stub) if stub else None,
            ) as rebuilt:
                _write_repacked(rebuilt, original, replacements, sink, token)
            staging.replace(destination)
        return destination

    def _game_root(
        self,
        game_path: Path,
        sink: ProgressSink = _NULL_PROGRESS,
        token: CancellationToken = _NEVER_CANCELLED,
    ) -> Path:
        game_path = Path(game_path)
        if game_path.is_dir():
            return game_path
        if game_path.is_file():
            return self._unpack_archive(game_path, sink, token)
        raise FileNotFoundError(f"Game path does not exist: {game_path}")

    def _unpack_archive(
        self,
        archive_path: Path,
        sink: ProgressSink,
        token: CancellationToken,
    ) -> Path:
        """Extract a LiveMaker archive (``.exe``/``.dat``) into the workspace."""
        root = self.workspace or archive_path.parent
        root.mkdir(parents=True, exist_ok=True)
        workspace = root.resolve()
        dest = _resolved_cache_path(workspace, root / archive_path.stem)
        staging = _resolved_cache_path(
            workspace, root / f"{archive_path.stem}.partial"
        )
        digest = archive_digest(archive_path)
        if holds_original(dest, archive_path, digest) and any(dest.rglob("*.lsb")):
            return dest
        promoted = False
        with LMArchive(str(archive_path)) as archive:
            entries = archive.infolist()
            safe_parts = [_validated_archive_parts(entry.path) for entry in entries]
            try:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                staging.mkdir(parents=True)
                failures = _extract_entries(archive, entries, safe_parts, staging, sink, token)
                if failures:
                    details = "; ".join(f"{path}: {error}" for path, error in failures)
                    raise LiveMakerException(
                        f"Failed to unpack LiveMaker archive {archive_path}: {details}. "
                        "The incomplete cache was discarded; fix the archive and retry."
                    ) from failures[0][1]
                if dest.exists():
                    shutil.rmtree(dest)
                staging.replace(dest)
                promoted = True
                unpacked_stamp(dest).write_text(digest, encoding="ascii")
                return dest
            finally:
                if not promoted and staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _open(lsb_path: Path) -> LMScript:
        try:
            pylm = PylmProject(lsb_path)
            call_name = pylm.call_name(lsb_path)
        except LiveMakerException:
            pylm = None
            call_name = None
        with open(lsb_path, "rb") as f:
            return _IndexedScript.from_file(f, call_name=call_name, pylm=pylm)

    @staticmethod
    def _serialize_validated(lsb: LMScript) -> bytes:
        """Serialize and re-parse to guarantee the output is not corrupt."""
        try:
            data = _IndexedScript._struct().build(lsb)
        except construct.ConstructError as error:
            raise BadLsbError(error) from error
        _IndexedScript.from_lsb(data)
        return data


def _repacked_compression(compress_type):
    """An encrypted entry is written back plain: the rebuilt archive holds no key."""
    return {
        LMCompressType.ENCRYPTED: LMCompressType.NONE,
        LMCompressType.ENCRYPTED_ZLIB: LMCompressType.ZLIB,
    }.get(compress_type, compress_type)


def _overlay_replacements(entries: list, overlay: Path) -> list[tuple[object, Path]]:
    """Each archive entry with the overlay path that would replace it."""
    overlay_root = overlay.resolve()
    replacements = []
    for entry in entries:
        replacement = overlay_root.joinpath(*_validated_archive_parts(entry.path)).resolve()
        if not replacement.is_relative_to(overlay_root):
            raise ValueError(f"LiveMaker archive entry escapes its overlay: {entry.path}")
        replacements.append((entry, replacement))
    return replacements


def _write_repacked(
    rebuilt: LMArchive,
    original: LMArchive,
    replacements: list[tuple[object, Path]],
    sink: ProgressSink,
    token: CancellationToken,
) -> None:
    total = len(replacements)
    for index, (entry, replacement) in enumerate(replacements, start=1):
        token.raise_if_cancelled()
        if replacement.is_file():
            rebuilt.write(
                replacement,
                compress_type=_repacked_compression(entry.compress_type),
                unk1=entry.unk1,
                arcname=entry.path,
            )
        else:
            rebuilt.writebytes(entry, original.read(entry, decompress=False))
        if index % 200 == 0 or index == total:
            sink.publish(
                ProgressEvent(
                    "engine.repack", index, total, "engine.repack.progress", detail=str(entry.path)
                )
            )


def _extract_entries(
    archive: LMArchive,
    entries: list,
    safe_parts: list[tuple[str, ...]],
    staging: Path,
    sink: ProgressSink,
    token: CancellationToken,
) -> list[tuple[object, Exception]]:
    """Extract each entry into ``staging``; the entries that could not be, with why."""
    failures = []
    total = len(entries)
    for index, (entry, parts) in enumerate(zip(entries, safe_parts, strict=True), start=1):
        token.raise_if_cancelled()
        target = staging.joinpath(*parts).resolve()
        if not target.is_relative_to(staging):
            raise ValueError(f"LiveMaker archive entry escapes staging: {entry.path}")
        try:
            archive.extract(entry, staging)
        except (LiveMakerException, OSError) as error:
            failures.append((entry.path, error))
            _log.warning("Skipped archive entry %s: %s", entry.path, error)
            sink.publish(
                ProgressEvent(
                    "engine.unpack",
                    index,
                    total,
                    "engine.unpack.entry.skipped",
                    level=ProgressLevel.WARNING,
                    detail=str(error),
                )
            )
            continue
        if index % 100 == 0 or index == total:
            sink.publish(
                ProgressEvent(
                    "engine.unpack", index, total, "engine.unpack.progress", detail=str(entry.path)
                )
            )
    return failures


def _status_font_assignment(face: str, line: int) -> Calc:
    """A ``Calc`` setting ``StatusFontName`` to ``face``, at ``line``."""
    assignment = Calc(LineNo=line)
    assignment.args["Calc"] = LiveParser(
        entries=[
            OpeData(OpeDataType.To, "____0", operands=[Param(face, ParamType.Str)]),
            OpeData(
                OpeDataType.To,
                _STATUS_FONT.decode(),
                operands=[Param("____0", ParamType.Var)],
            ),
        ]
    )
    return assignment


def _shift_labels(lsb: LMScript, page: PureWindowsPath, line: int) -> bool:
    """Move each jump into ``page`` past ``line`` down one line; whether any moved."""
    moved = False
    for command in lsb.commands:
        for value in command.args.values():
            if (
                isinstance(value, LabelReference)
                and PureWindowsPath(value.Page).with_suffix("") == page
                and value.Label > line
            ):
                value.Label += 1
                moved = True
    return moved
