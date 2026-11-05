"""Which string values a LiveMaker game shows, followed from where they are assigned."""

from __future__ import annotations

import hashlib
import itertools
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from livemaker.lsb.core import OpeDataType, ParamType
from livemaker.lsb.novel import TWdOpeVar

from bokucho.findings import Finding

SHOWN = ("<shown>",)
KEYED = ("<keyed>",)
MENU_ARRAY = "_tmp"
MENU = ("", MENU_ARRAY, "w")
CAPTION = 50
TEXT = "PR_TEXT"
VALUE_ID = "pylm:value:"

EXPRESSIONS = frozenset(
    {"Calc", "If", "Elseif", "While", "WhileInit", "WhileLoop", "Jump", "Wait"}
)
READING = frozenset(
    {
        "JLength",
        "Length",
        "JPos",
        "Pos",
        "StrToIntDef",
        "StrToFloatDef",
        "Int",
        "Float",
        "Ord",
        "VarExists",
        "GetArraySize",
        "SetArray",
    }
)
IN_PLACE = frozenset({"JInsert", "JDelete", "Insert", "Delete"})
CUTTING = frozenset({"JCopy", "Copy", "Trim", "JTrim", "FixStr"})
SPLITTING = frozenset({"StringToArray"})
CARRYING = {"StringReplace": (0, 2), "ArrayToString": (0,)}
JOIN_LIMIT = 100_000

_ELEMENT = re.compile(r"([^\[\]]+)((?:\[\d+\])*)")


def value_id(word: str) -> str:
    """The unit id of a value: the same word is the same unit, game-wide."""
    return VALUE_ID + hashlib.sha256(word.encode("utf-8")).hexdigest()[:16]


def worded(word: str) -> bool:
    """Whether a word has anything to translate: a character outside ASCII."""
    return any(ord(ch) > 127 for ch in word)


@dataclass(frozen=True)
class Flow:
    """What an expression's value may be: the values of places, and literals."""

    places: frozenset = frozenset()
    words: tuple = ()


NOTHING = Flow()


def _union(flows: Iterable[Flow]) -> Flow:
    flows = list(flows)
    return Flow(
        frozenset().union(*(flow.places for flow in flows)),
        sum((flow.words for flow in flows), ()),
    )


@dataclass
class Site:
    """A string literal assigned to a place."""

    script: str
    line: int
    place: tuple
    word: str
    operand: object


@dataclass
class Compare:
    """A literal compared with the values of places: 装備名[6] == "旅人の服"."""

    script: str
    command: object
    operand: object
    word: str
    places: frozenset


@dataclass
class Needle:
    """JPos(needle, haystack)."""

    script: str
    command: object
    needle: Flow
    haystack: Flow
    group: int | None = None


@dataclass
class Replace:
    """StringReplace(subject, find, replacement)."""

    script: str
    command: object
    subject: Flow
    find: Flow
    replacement: Flow


@dataclass
class Cut:
    """JDelete(subject, p) where p = JPos(needle, subject): the subject cut before the needle."""

    script: str
    line: int
    subject: Flow
    needle: Flow


@dataclass
class Join:
    """Pieces joined by ++ and assigned to a place."""

    script: str
    line: int
    pieces: list
    target: tuple


@dataclass
class Values:
    """The value flow of a game, and what the game does with its string literals."""

    edges: dict = field(default_factory=lambda: defaultdict(set))
    back: dict = field(default_factory=lambda: defaultdict(set))
    sites: list = field(default_factory=list)
    compares: list = field(default_factory=list)
    needles: list = field(default_factory=list)
    replaces: list = field(default_factory=list)
    cuts: list = field(default_factory=list)
    joins: list = field(default_factory=list)
    fixed: set = field(default_factory=set)
    shown: set = field(default_factory=set)
    keyed: set = field(default_factory=set)
    reached: set = field(default_factory=set)

    def translatable(self, site: Site) -> bool:
        return site.place in self.shown and site.place not in self.keyed

    def reaches(self, flow: Flow) -> bool:
        """Whether translatable values may be in ``flow``."""
        return not flow.places.isdisjoint(self.reached)

    def words(self) -> dict[str, set[str]]:
        """Each word to translate, and the variables it is assigned to or checked on."""
        found: dict[str, set[str]] = defaultdict(set)

        def add(word: str, places: Iterable) -> None:
            if worded(word):
                found[word].update(p[1] for p in places if len(p) == 3 and p[0] != "<cut>")

        for site in self.sites:
            if self.translatable(site):
                add(site.word, [site.place])
        for compare in self.compares:
            if not compare.places.isdisjoint(self.reached):
                add(compare.word, compare.places & self.reached)
        for needle in self.needles:
            if self.reaches(needle.haystack):
                for word, _ in needle.needle.words:
                    add(word, needle.haystack.places & self.reached)
        for replace in self.replaces:
            if self.reaches(replace.subject):
                for word, _ in replace.find.words + replace.replacement.words:
                    add(word, replace.subject.places & self.reached)
        return dict(found)

    def values_at(self, places: Iterable) -> dict[str, set[bool]]:
        """Each word that may reach ``places``, and whether its sites there are translatable."""
        seen = _closure(places, self.back)
        found: dict[str, set[bool]] = defaultdict(set)
        for site in self.sites:
            if site.place in seen:
                found[site.word].add(self.translatable(site))
        return dict(found)

    def offered(self) -> set[str]:
        """Words a translatable site of which may end up in a menu."""
        to_menu = _closure([MENU], self.back)
        return {s.word for s in self.sites if self.translatable(s) and s.place in to_menu}

    def excluded(self) -> dict[str, set[str]]:
        """Words assigned where they are shown but also used as a key: they stay as they are."""
        found: dict[str, set[str]] = defaultdict(set)
        for site in self.sites:
            if worded(site.word) and site.place in self.shown and site.place in self.keyed:
                found[site.word].add(site.place[1])
        return dict(found)


def _closure(start: Iterable, edges: dict) -> set:
    seen = set(start)
    pending = list(seen)
    while pending:
        for node in edges.get(pending.pop(), ()):
            if node not in seen:
                seen.add(node)
                pending.append(node)
    return seen


def _func(entry) -> str:
    return getattr(entry.func, "name", None) or str(entry.func)


def _held(op) -> str | None:
    """The variable an operand reads, if it reads one."""
    return op.value if op is not None and int(op.type) == ParamType.Var else None


def _constant(parser) -> int | None:
    """The integer an expression is, when it is only one."""
    entries = getattr(parser, "entries", None) or []
    if len(entries) != 1 or len(entries[0].operands) != 1:
        return None
    op = entries[0].operands[0]
    return int(op.value) if op is not None and int(op.type) == ParamType.Int else None


def _found_any(parser) -> bool:
    """Whether ``parser`` only asks if any of its searches found its needle."""
    searches: set = set()
    found: set = set()
    answered = False
    for entry in parser.entries:
        kind = int(entry.type)
        ops = [op for op in entry.operands if op is not None]
        held = [_held(op) for op in ops]
        if kind == OpeDataType.Func and _func(entry) == "JPos":
            searches.add(entry.name)
        elif (
            kind == OpeDataType.EBig
            and len(ops) == 2
            and held[0] in searches
            and int(ops[1].type) == ParamType.Int
            and int(ops[1].value) == 1
        ):
            found.add(entry.name)
        elif kind == OpeDataType.Or and held and all(name in found for name in held):
            found.add(entry.name)
        elif kind == OpeDataType.To and entry.name == "____arg" and len(held) == 1:
            answered = held[0] in found
        elif kind not in (OpeDataType.To, OpeDataType.DimTo) or not searches.union(
            found
        ).isdisjoint(held):
            return False
    return answered


def _names(script) -> tuple[set, set, set]:
    """The variables ``script`` uses, those it asks VarExists about, and the scripts it runs."""
    used: set = set()
    asked: set = set()
    runs: set = set()
    for command in script.commands:
        page = getattr(command.args.get("Page"), "Page", None)
        if isinstance(page, str):
            runs.add(page.replace("\\", "/"))
        text = command.args.get("Text")
        for glyph in getattr(text, "body", None) or []:
            if isinstance(glyph, TWdOpeVar):
                used.add(glyph.name.split("[")[0])
        for arg in command.args.values():
            for parser in getattr(arg, "parsers", None) or (
                [arg] if getattr(arg, "entries", None) else []
            ):
                strings = {}
                for entry in parser.entries:
                    if not entry.name.startswith("____"):
                        used.add(entry.name)
                    for op in entry.operands:
                        name = _held(op)
                        if name and not name.startswith("____"):
                            used.add(name)
                        elif op is not None and int(op.type) == ParamType.Str:
                            strings[entry.name] = op.value
                    if _func(entry) == "VarExists":
                        asked.update(strings.get(_held(op), "") for op in entry.operands)
    return used, asked, runs


def _own(scripts: dict) -> dict:
    """Each script's own variables: the locals no other script can see."""
    declared: dict = {}
    local: dict = {}
    used: dict = {}
    asked: dict = {}
    runs: dict = {}
    for name, script in scripts.items():
        news = [c for c in script.commands if type(c).__name__ == "VarNew"]
        declared[name] = {c.args.get("Name") for c in news}
        local[name] = {c.args.get("Name") for c in news if int(c.args.get("Scope") or 0) in (1, 2)}
        used[name], asked[name], runs[name] = _names(script)
    calls = {name: runs[name] & scripts.keys() for name in scripts}
    own = {}
    for name in scripts:
        seen = set()
        for other in _closure(calls[name], calls) - {name}:
            seen |= used[other] - declared[other]
        own[name] = local[name] - asked[name] - seen
    return own


def _assigned(parser) -> tuple[str, int] | None:
    """``v = c``: the variable and the integer, when that is all the statement is."""
    entries = getattr(parser, "entries", None) or []
    if len(entries) != 1 or entries[0].name.startswith("____"):
        return None
    value = _constant(parser)
    return None if value is None else (entries[0].name, value)


def _steps_up(parser, var: str) -> bool:
    """``v = v + k``, k not negative."""
    entries = getattr(parser, "entries", None) or []
    if len(entries) != 2:
        return False
    step, to = entries
    ops = [op for op in step.operands if op is not None]
    return (
        int(step.type) == OpeDataType.Plus
        and len(ops) == 2
        and _held(ops[0]) == var
        and int(ops[1].type) == ParamType.Int
        and int(ops[1].value) >= 0
        and int(to.type) == OpeDataType.To
        and to.name == var
        and [_held(op) for op in to.operands if op is not None] == [step.name]
    )


def _writes(command, var: str) -> bool:
    if type(command).__name__ == "VarNew" and command.args.get("Name") == var:
        return True
    for arg in command.args.values():
        for parser in getattr(arg, "parsers", None) or (
            [arg] if getattr(arg, "entries", None) else []
        ):
            if any(entry.name == var for entry in parser.entries):
                return True
    return False


def _floors(script, own: Iterable[str]) -> dict[int, dict[str, int]]:
    """Per command (by id), the least value each loop counter around it holds."""
    # ponytail: a Jump into the middle of a loop body would break the floor;
    # LiveMaker's editor builds loops as blocks, and no game here jumps in.
    floors: dict[int, dict[str, int]] = defaultdict(dict)
    commands = script.commands
    for start, command in enumerate(commands):
        if type(command).__name__ != "While" or start == 0:
            continue
        before = commands[start - 1]
        counter = (
            _assigned(before.args.get("Calc")) if type(before).__name__ == "WhileInit" else None
        )
        if counter is None:
            continue
        var, least = counter
        if var not in own:
            continue
        end = next(
            (
                index
                for index in range(start + 1, len(commands))
                if type(commands[index]).__name__ == "WhileLoop"
                and commands[index].args.get("Start") == start
            ),
            None,
        )
        if end is None or not _steps_up(commands[end].args.get("Calc"), var):
            continue
        body = commands[start + 1 : end]
        if any(_writes(inner, var) for inner in body):
            continue
        for inner in body:
            floors[id(inner)][var] = least
    return floors


def _symbolic(part) -> bool:
    """A part naming elements by a rule (from the end, from a floor), not by number."""
    return isinstance(part, tuple) and bool(part) and isinstance(part[0], tuple)


def analyse(scripts: dict, decode: Callable[[str], str] = str) -> Values:
    """Follow every string literal of ``scripts``, keyed by path as a Call names them."""
    return _Analysis(scripts, decode).run()


class _Analysis:
    def __init__(self, scripts: dict, decode: Callable[[str], str]) -> None:
        self.scripts = scripts
        self.decode = decode
        self.values = Values()
        self.own = _own(scripts)
        self.floors: dict = {}
        for name, script in scripts.items():
            self.floors.update(_floors(script, self.own.get(name, ())))
        self.arrays: dict = defaultdict(set)
        self.cut_nodes: list = []
        self.positions: dict = defaultdict(list)
        self.deletes: list = []
        self.built: list = []
        self.rebuilt: set = set()

    def place(self, script: str, name: str, part) -> tuple:
        scope = script if name == "@ParamStr" or name in self.own.get(script, ()) else ""
        node = (scope, name, part)
        self.arrays[(scope, name)].add(node)
        return node

    def element(self, script: str, alias: tuple, mode: str) -> tuple:
        name, index = alias
        if index is None or (mode == "w" and _symbolic(index)):
            return self.place(script, name, mode)
        return self.place(script, name, index)

    def index(self, command, array: str, index: list, ends: dict) -> tuple | None:
        """The part of ``array`` an index names: numbers, a rule, or None for any."""
        if all(op is not None and int(op.type) == ParamType.Int for op in index):
            return tuple(int(op.value) for op in index)
        if len(index) != 1 or array != "@ParamStr":
            return None
        held = _held(index[0])
        if held in ends:
            return (("end", ends[held]),)
        floor = self.floors.get(id(command), {}).get(held)
        return None if floor is None else ((">=", floor),)

    def assign(self, script: str, command, target: tuple, flow: Flow) -> None:
        if target == MENU and (flow.places or len(flow.words) > 1):
            target = ("", MENU_ARRAY, ("built", script, command.LineNo))
            self.values.edges[target].add(MENU)
            self.built.append((target, flow))
        for place in flow.places:
            self.values.edges[place].add(target)
        if target == MENU:
            return
        for word, operand in flow.words:
            self.values.sites.append(Site(script, command.LineNo, target, word, operand))

    def key(self, flow: Flow) -> None:
        for place in flow.places:
            self.values.edges[place].add(KEYED)
        self.values.fixed.update(id(operand) for _, operand in flow.words)

    def walk(self, script: str, command, parser) -> Flow:
        """The value of ``parser``, recording what it does with the values in it."""
        first_needle = len(self.values.needles)
        expression = _Expression(self, script, command)
        for entry in parser.entries:
            expression.step(entry)
        made = self.values.needles[first_needle:]
        if (
            len(made) > 1
            and all(not n.needle.places and len(n.needle.words) == 1 for n in made)
            and len({n.haystack.places for n in made}) == 1
            and _found_any(parser)
        ):
            for needle in made:
                needle.group = first_needle
        return expression.last


    def call(self, script: str, command, entry, ops: list, flows: list, jpos: dict) -> Flow:
        name = _func(entry)
        if name == "AddArray":
            array = ops[0].value
            target = MENU if array == MENU_ARRAY else self.place(script, array, "w")
            for flow in flows[1:]:
                self.assign(script, command, target, flow)
            return NOTHING
        if name == "JPos" and len(flows) >= 2:
            self.values.needles.append(Needle(script, command, flows[0], flows[1]))
            jpos[entry.name] = (flows[0], flows[1])
            return NOTHING
        if name == "JDelete" and len(ops) == 2:
            position = _held(ops[1])
            if position in jpos:
                needle, haystack = jpos[position]
                if haystack.places == flows[0].places:
                    self.values.cuts.append(Cut(script, command.LineNo, flows[0], needle))
            elif position is not None:
                self.deletes.append((script, command.LineNo, flows[0], position))
            return NOTHING
        if name in READING or name in IN_PLACE:
            return NOTHING
        if name in CUTTING or name in SPLITTING:
            node = ("<cut>", script, len(self.cut_nodes))
            self.cut_nodes.append(node)
            self.assign(script, command, node, flows[0] if flows else NOTHING)
            if name in CUTTING:
                return Flow(frozenset({node}))
            array = _held(ops[1]) if len(ops) > 1 else None
            if array is not None:
                target = self.place(script, array, "w")
                self.assign(script, command, target, Flow(frozenset({node})))
            return NOTHING
        if name in CARRYING:
            if name == "StringReplace":
                subject, find, replacement = (flows + [NOTHING] * 3)[:3]
                self.values.replaces.append(
                    Replace(script, command, subject, find, replacement)
                )
            return _union(flows[i] for i in CARRYING[name] if i < len(flows))
        for flow in flows:
            self.key(flow)
        return NOTHING

    def compare(self, script: str, command, flows: list) -> None:
        if len(flows) != 2:
            return
        for mine, theirs in (flows, flows[::-1]):
            if not theirs.places or not mine.words:
                continue
            if mine.places or len(mine.words) > 1:
                self.key(theirs)
                self.rebuilt.update(word for word, _ in mine.words)
                continue
            ((word, operand),) = mine.words
            self.values.compares.append(Compare(script, command, operand, word, theirs.places))

    def command(self, script: str, command) -> None:
        kind = type(command).__name__
        args = command.args
        if kind in EXPRESSIONS:
            for arg in args.values():
                if getattr(arg, "entries", None):
                    self.walk(script, command, arg)
            return
        if kind == "VarNew":
            if getattr(args.get("InitVal"), "entries", None):
                flow = self.walk(script, command, args["InitVal"])
                self.assign(script, command, self.place(script, args["Name"], "w"), flow)
            return
        if kind == "Call":
            callee = getattr(args.get("Page"), "Page", "").replace("\\", "/")
            params = getattr(args.get("Params"), "parsers", None) or []
            for index, parser in enumerate(params):
                if not getattr(parser, "entries", None):
                    continue
                flow = self.walk(script, command, parser)
                if callee in self.scripts:
                    target = self.place(callee, "@ParamStr", (index,))
                    self.assign(script, command, target, flow)
                    # ponytail: a callee's own write to @ParamStr[j] reaches the
                    # end-counted reads only through this call's j.
                    end = self.place(callee, "@ParamStr", (("end", len(params) - index),))
                    self.values.edges[target].add(end)
                else:
                    self.key(flow)
            if getattr(args.get("Calc"), "entries", None):
                self.walk(script, command, args["Calc"])
            return
        if kind == "TextIns":
            for glyph in getattr(args.get("Text"), "body", None) or []:
                if isinstance(glyph, TWdOpeVar):
                    self.values.edges[self.shown_var(script, glyph.name)].add(SHOWN)
            return
        shown = {"Caption"} if kind == "GameSave" else set()
        if TEXT in args:
            shown.add(TEXT)
        if kind == "SetProp" and _constant(args.get("ObjProp")) == CAPTION:
            shown.add("Value")
        for name, arg in args.items():
            parsers = getattr(arg, "parsers", None) or (
                [arg] if getattr(arg, "entries", None) else []
            )
            for parser in parsers:
                flow = self.walk(script, command, parser)
                if name in shown:
                    self.assign(script, command, SHOWN, flow)
                else:
                    self.key(flow)

    def shown_var(self, script: str, name: str) -> tuple:
        """The place a <VAR name> glyph shows."""
        match = _ELEMENT.fullmatch(name)
        if match and match.group(2):
            index = tuple(int(i) for i in re.findall(r"\d+", match.group(2)))
            return self.place(script, match.group(1), index)
        return self.place(script, name.split("[")[0], "r")

    def run(self) -> Values:
        values = self.values
        for name, script in self.scripts.items():
            for command in script.commands:
                self.command(name, command)
        for script, line, subject, var in self.deletes:
            for needle, haystack in self.positions.get((script, var), ()):
                if haystack.places == subject.places:
                    values.cuts.append(Cut(script, line, subject, needle))
        for (scope, name), nodes in self.arrays.items():
            read, write = (scope, name, "r"), (scope, name, "w")
            values.edges[write].add(read)
            for node in nodes:
                if isinstance(node[2], tuple):
                    values.edges[node].add(read)
                    values.edges[write].add(node)
            for floor in [n for n in nodes if _symbolic(n[2]) and n[2][0][0] == ">="]:
                for node in nodes:
                    part = node[2]
                    if isinstance(part, tuple) and len(part) == 1 and isinstance(part[0], int):
                        if part[0] >= floor[2][0][1]:
                            values.edges[node].add(floor)
        values.edges[MENU].add(SHOWN)
        for node, flow in self.built:
            if any(word in self.rebuilt for word, _ in flow.words):
                values.edges[node].add(KEYED)
        for node in self.cut_nodes:
            after = _closure([node], values.edges)
            if any(c.word and not after.isdisjoint(c.places) for c in values.compares):
                values.edges[node].add(KEYED)
        for source, targets in values.edges.items():
            for target in targets:
                values.back[target].add(source)
        values.shown = _closure([SHOWN], values.back)
        values.keyed = _closure([KEYED], values.back)
        values.reached = _closure(
            {site.place for site in values.sites if values.translatable(site)}, values.edges
        )
        values.fixed.update(
            id(site.operand) for site in values.sites if not values.translatable(site)
        )
        return values


class _Expression:
    """One expression's temporaries, followed entry by entry."""

    def __init__(self, analysis: _Analysis, script: str, command) -> None:
        self.analysis = analysis
        self.script = script
        self.command = command
        self.alias: dict = {}
        self.temps: dict = {}
        self.jpos: dict = {}
        self.pieces: dict = {}
        self.ends: dict = {}
        self.last = NOTHING

    def value(self, op) -> Flow:
        if op is None:
            return NOTHING
        if int(op.type) == ParamType.Str:
            return Flow(words=((self.analysis.decode(op.value), op),))
        name = _held(op)
        if name is None or name == MENU_ARRAY:
            return NOTHING
        if name in self.temps:
            return self.temps[name]
        if name in self.alias:
            return Flow(frozenset({self.analysis.element(self.script, self.alias[name], "r")}))
        return Flow(frozenset({self.analysis.place(self.script, name, "r")}))

    def step(self, entry) -> None:
        kind = int(entry.type)
        ops = list(entry.operands)
        if kind == OpeDataType.DimTo:
            self.alias[entry.name] = (
                ops[0].value,
                self.analysis.index(self.command, ops[0].value, ops[1:], self.ends),
            )
            return
        if (
            kind == OpeDataType.Minus
            and len(ops) == 2
            and _held(ops[0]) == "@ParamCount"
            and ops[1] is not None
            and int(ops[1].type) == ParamType.Int
        ):
            self.ends[entry.name] = int(ops[1].value)
        if kind == OpeDataType.To and len(ops) == 1:
            self._assign(entry.name, ops[0])
            return
        self.last = self._operate(kind, entry, ops)
        self.temps[entry.name] = self.last

    def _assign(self, name: str, op) -> None:
        analysis = self.analysis
        flow = self.last = self.value(op)
        source = _held(op)
        if name.startswith("____") and name not in self.alias:
            self.temps[name] = flow
            if source in self.jpos:
                self.jpos[name] = self.jpos[source]
            if source in self.pieces:
                self.pieces[name] = self.pieces[source]
            return
        if name in self.alias:
            target = analysis.element(self.script, self.alias[name], "w")
        elif name == MENU_ARRAY:
            target = MENU
        else:
            target = analysis.place(self.script, name, "w")
            if source in self.jpos:
                analysis.positions[(self.script, name)].append(self.jpos[source])
        analysis.assign(self.script, self.command, target, flow)
        if source in self.pieces:
            analysis.values.joins.append(
                Join(self.script, self.command.LineNo, self.pieces[source], target)
            )

    def _operate(self, kind: int, entry, ops: list) -> Flow:
        flows = [self.value(op) for op in ops]
        if kind == OpeDataType.Func:
            return self.analysis.call(self.script, self.command, entry, ops, flows, self.jpos)
        if kind in (OpeDataType.Equal, OpeDataType.NEqual):
            self.analysis.compare(self.script, self.command, flows)
            return NOTHING
        if kind in (OpeDataType.Plus, OpeDataType.ComboStr):
            self.pieces[entry.name] = [
                part
                for op, flow in zip(ops, flows, strict=True)
                for part in self.pieces.get(_held(op), [flow])
            ]
            return _union(flows)
        return NOTHING


def inconsistencies(values: Values, translate: Callable[[str], str]) -> list[str]:
    """Where a value the game derives would no longer be the word it is checked against."""
    return [finding.message for finding, _, _ in mismatches(values, translate)]


def assembled(
    values: Values,
    translate: Callable[[str], str],
    fixable: Callable[[str], bool],
) -> dict[str, str]:
    """Translations that make the values the game derives match again."""
    parts = (
        {site.word for site in values.sites}
        | {compare.word for compare in values.compares}
        | set(values.words())
    )
    fixes: dict[str, str] = {}

    def current(word: str) -> str:
        return fixes[word] if word in fixes else translate(word)

    # ponytail: a few rounds, since a value put right can be a piece of another;
    # more if a game nests its values deeper.
    for _ in range(4):
        wanted: dict[str, set[str]] = defaultdict(set)
        for _, whole, expected in mismatches(values, current):
            if not fixable(whole):
                continue
            if expected is None:
                expected = _side_by_side(whole, parts, current)
            if expected is not None:
                wanted[whole].add(expected)
        found = {
            whole: next(iter(expected))
            for whole, expected in wanted.items()
            if len(expected) == 1 and expected != {current(whole)}
        }
        if not found:
            break
        fixes.update(found)
    return {word: target for word, target in fixes.items() if target != translate(word)}


def _side_by_side(word: str, parts: set[str], translate: Callable[[str], str]) -> str | None:
    """``word`` as the translations of the two words it is, if it is two in one way only."""
    splits = {
        translate(word[:at]) + translate(word[at:])
        for at in range(1, len(word))
        if word[:at] in parts and word[at:] in parts
    }
    return splits.pop() if len(splits) == 1 else None


def mismatches(values: Values, translate: Callable[[str], str]):
    """Each inconsistency: what it found, the value derived, and its translation if one follows."""
    check = _Mismatches(values, translate)
    yield from check.group_searches()
    yield from check.searches()
    yield from check.cuts()
    yield from check.replaces()
    yield from check.joins()


class _Mismatches:
    """The checks ``mismatches`` runs, each over one kind of string operation."""

    def __init__(self, values: Values, translate: Callable[[str], str]) -> None:
        self.values = values
        self.translate = translate
        self.known = {site.word for site in values.sites} | {c.word for c in values.compares}

    def either(self, word: str) -> set[str]:
        return {word, self.translate(word)}

    def written(self, word: str) -> set[str]:
        return {self.translate(word)}

    @staticmethod
    def kept(word: str) -> set[str]:
        return {word}

    @staticmethod
    def unit(word: str) -> str:
        return f"{word!r} ({value_id(word)})" if worded(word) else repr(word)

    def forms(self, flow: Flow, literal: Callable[[str], set[str]]) -> dict[str, set[str]]:
        """Each value ``flow`` may be, and what the game holds for it after the inject."""
        found: dict[str, set[str]] = defaultdict(set)
        for word, flags in self.values.values_at(flow.places).items():
            found[word].update(self.translate(word) if flag else word for flag in flags)
        for word, _ in flow.words:
            found[word].update(literal(word))
        return found

    def group_searches(self):
        groups: dict = defaultdict(list)
        for needle in self.values.needles:
            if needle.group is not None:
                groups[needle.group].append(needle)
        for members in groups.values():
            if self.values.reaches(members[0].haystack):
                yield from self._group_search(members)

    def _group_search(self, members: list[Needle]):
        unit = self.unit
        where = f"{members[0].script}:{members[0].command.LineNo}"
        words = sorted({member.needle.words[0][0] for member in members})
        searched = set().union(*(self.either(word) for word in words))
        for value, held in self.forms(members[0].haystack, self.either).items():
            hit = any(word in value for word in words)
            for form in held:
                if hit != any(f in form for f in searched):
                    details = {
                        "where": where,
                        "value": unit(value),
                        "words": ", ".join(map(unit, words)),
                        "forms": ", ".join(map(repr, sorted(searched))),
                        "form": repr(form),
                    }
                    kind = "values_search_any_found" if hit else "values_search_any_missing"
                    yield Finding("values", kind, details), value, None

    def searches(self):
        for needle in self.values.needles:
            if needle.group is None and self.values.reaches(needle.haystack):
                yield from self._search(needle)

    def _search(self, needle: Needle):
        unit = self.unit
        searched = self.forms(needle.needle, self.either)
        for value, held in self.forms(needle.haystack, self.either).items():
            for word, found in searched.items():
                for form in held:
                    if (word in value) != any(f in form for f in found):
                        details = {
                            "where": f"{needle.script}:{needle.command.LineNo}",
                            "value": unit(value),
                            "word": unit(word),
                            "forms": ", ".join(map(repr, sorted(found))),
                            "form": repr(form),
                        }
                        kind = "values_search_found" if word in value else "values_search_missing"
                        yield Finding("values", kind, details), value, None

    def cuts(self):
        for cut in self.values.cuts:
            if self.values.reaches(cut.subject):
                yield from self._cut(cut)

    def _cut(self, cut: Cut):
        unit = self.unit
        searched = self.forms(cut.needle, self.either)
        for value, held in self.forms(cut.subject, self.either).items():
            for word, found in searched.items():
                part = value[: value.find(word)] if word and word in value else value
                if part not in self.known:
                    continue
                for form in held:
                    at = max(form.find(f) for f in found)
                    got = form[:at] if at >= 0 else form
                    if got not in self.either(part):
                        details = {
                            "where": f"{cut.script}:{cut.line}",
                            "value": unit(value),
                            "word": unit(word),
                            "part": unit(part),
                            "form": repr(form),
                            "got": repr(got),
                        }
                        expected = self.translate(part) + form[at:] if at >= 0 else None
                        yield Finding("values", "values_cut", details), value, expected

    def replaces(self):
        for replace in self.values.replaces:
            if not self.values.reaches(replace.subject):
                continue
            finds = self.forms(replace.find, self.written)
            news = self.forms(replace.replacement, self.written)
            for value, held in self.forms(replace.subject, self.either).items():
                for old, game_olds in finds.items():
                    if old and old in value:
                        yield from self._replace(replace, value, held, old, game_olds, news)

    def _replace(
        self,
        replace: Replace,
        value: str,
        held: set[str],
        old: str,
        game_olds: set[str],
        news: dict[str, set[str]],
    ):
        unit = self.unit
        for new, game_news in news.items():
            result = value.replace(old, new)
            if result not in self.known:
                continue
            for form, game_old, game_new in itertools.product(held, game_olds, game_news):
                got = form.replace(game_old, game_new) if game_old else form
                if got in self.either(result):
                    continue
                expected = None
                if not new and value == result + old:
                    expected = self.translate(result) + game_old
                elif not new and value == old + result:
                    expected = game_old + self.translate(result)
                details = {
                    "where": f"{replace.script}:{replace.command.LineNo}",
                    "old": unit(old),
                    "new": unit(new),
                    "value": unit(value),
                    "result": unit(result),
                    "form": repr(form),
                    "got": repr(got),
                }
                yield Finding("values", "values_replace", details), value, expected

    def joins(self):
        checked: dict = defaultdict(set)
        for compare in self.values.compares:
            for place in compare.places & self.values.reached:
                checked[place].add(compare.word)
        for join in self.values.joins:
            if join.target not in self.values.reached:
                continue
            after = _closure([join.target], self.values.edges)
            wanted = set().union(*(checked[place] for place in after if place in checked))
            if wanted:
                yield from self._join(join, wanted)

    def _join(self, join: Join, wanted: set[str]):
        shown = join.target in self.values.shown and join.target not in self.values.keyed
        parts = [self.forms(piece, self.written if shown else self.kept) for piece in join.pieces]
        if not all(parts):
            return
        # ponytail: tries at most JOIN_LIMIT joins of piece values; a bigger join goes
        # unchecked. Enumerate per wanted word instead if a game ever needs more.
        for combo in itertools.islice(
            itertools.product(*(part.items() for part in parts)), JOIN_LIMIT
        ):
            whole = "".join(word for word, _ in combo)
            if whole not in wanted:
                continue
            for game in itertools.product(*(held for _, held in combo)):
                if "".join(game) not in self.either(whole):
                    details = {
                        "where": f"{join.script}:{join.line}",
                        "pieces": " ++ ".join(self.unit(w) for w, _ in combo),
                        "whole": self.unit(whole),
                        "game": repr("".join(game)),
                    }
                    yield Finding("values", "values_join", details), whole, "".join(game)
                    break
