"""Ruby string literals in game scripts: found, decoded, encoded back and sorted by rules 1-5."""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum, auto


@dataclass(frozen=True)
class Literal:
    """A quoted literal: ``source[start:end]`` with quotes, ``raw`` inside, ``text`` as shown."""

    start: int
    end: int
    quote: str
    raw: str
    text: str
    line: int
    column: int


class UnreadableScript(ValueError):
    """A literal, comment, heredoc or regexp is not closed, or the brackets do not balance."""


class Rule(StrEnum):
    """What the code around a literal makes of it; rules 1-5, the first that matches."""

    SKIP = auto()
    KEY = auto()
    CODE = auto()
    DISPLAY = auto()
    OTHER = auto()


_BEG, _ARG, _END = range(3)
_BEGIN_WORDS = frozenset(
    {"when", "if", "unless", "while", "until", "and", "or", "not", "return"}
)
_WORD = re.compile(r"[^\W\d]\w*(?:[?!](?!=))?")
_NUMBER = re.compile(r"\d\w*(?:\.\d\w*)?")
_SIGIL = re.compile(r"[@$](?:@?\w+|[^\s\w])")
_PERCENT = re.compile(r"%(?:[qQwWiIrsx]([^\w\s])|([^\w\s=]))")
_HEREDOC = re.compile(r"""<<([-~]?)(?:([^\W\d]\w*)|(["'`])(.*?)\3)""")
_BLOCK = re.compile(r"=begin(?:[ \t\r]|$)", re.M)
_BLOCK_END = re.compile(r"^=end(?:[ \t\r]|$)", re.M)
_DATA = re.compile(r"__END__\r?$", re.M)
_PAIRS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_SPECIAL = {'"': re.compile(r'["\\#]'), "'": re.compile(r"['\\]")}
_ESCAPE = re.compile(
    r"\\(?:x([0-9a-fA-F]{1,2})|u([0-9a-fA-F]{4})"
    r"|u\{\s*([0-9a-fA-F]+(?:\s+[0-9a-fA-F]+)*)\s*\}|([0-7]{1,3})|(\r\n|.))",
    re.S,
)
#: Named escapes; a ``\`` before a newline (LF or CRLF) joins the lines, as Ruby does.
_NAMED = {
    "n": "\n", "t": "\t", "s": " ", "r": "\r", "e": "\x1b",
    "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\n": "", "\r\n": "",
}
_ID = r"A-Za-z_\x80-\U0010ffff"
#: The variable after ``#@``, ``#@@`` or ``#$`` that Ruby interpolates without braces.
_DVAR = re.compile(
    rf"""@@?[{_ID}][0-9{_ID}]*"""
    rf"""|\$(?:[{_ID}][0-9{_ID}]*|-[{_ID}]|[1-9][0-9]*|[~*$?!@/\\;,.=:<>"&`'+0])"""
)
_UNSAFE = re.compile(r'[\\"\x00-\x1f\x7f]|#(?=[$@{])')
# ponytail: no space flag, so prose such as "100% off" holds no fake "% o"; a real
# space-flag specifier such as "% d" is not protected. Add the space back, limited to
# literals used as a format, if a game needs it.
_SPEC = re.compile(r"%[-+0#*]*\d*(?:\.\d+)?[sdifgxXeEcbo%]")

#: The last alternative is an index, ``h["k"]``; a ``[`` after a space or operator opens an array.
_COMPARED_BEFORE = re.compile(
    r"(?:==|!=|\bwhen|\.(?:include\?|member\?|key\?|has_key\?|index|fetch)\(|\.g?sub!?\()\s*\Z"
    r"|\bwhen\s+(?:[^\s,()\[\]{}][^,()\[\]{}]*,\s*)+\Z"
    r"|[\w)\]]\[\s*\Z"
)
_COMPARED_AFTER = re.compile(r"\s*(?:==|!=|=>)")
_PATH = re.compile(
    r"(?i)\A(?:graphics|audio|data|fonts|movies)/"
    r"|\.(?:rxdata|rvdata2?|png|jpg|bmp|ogg|mid|wav|mp3|wma|txt|ini|dll|exe)\Z"
)
_ONE = r"""(?:"(?:\\.|[^"\\])"|'(?:\\.|[^'\\])')"""
_TABLE_BEFORE = re.compile(_ONE + r"\s*,\s*\Z")
_TABLE_AFTER = re.compile(r"\s*,\s*" + _ONE)
_CODE = re.compile(
    r"font\.name\s*=|Font\.default_name\s*=|Font\.new\(|\bFile(Test)?\.|Audio\.\w+_play|Cache\.|"
    r"Bitmap\.new\(|load_data\(|save_data\(|\brequire\b|\beval\b|\bsend\(|"
    r"(?i:^\s*[@$]{0,2}\w*font\w*)\s*=(?!=)"
)
#: Bytes no player reads, such as a file signature: C0 controls but tab and newlines, DEL, C1.
_BINARY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_DISPLAY = re.compile(
    r'draw_text|set_text|text_size|\bprint\b|\bp\(|super\(|@commands\s*=|\bs\d+\s*='
)
_CONSTANT = re.compile(r"\s*[A-Z]\w*\s*=")


def _line_end(text: str, at: int) -> int:
    end = text.find("\n", at)
    return len(text) if end < 0 else end


def _unescape(m: re.Match[str]) -> str:
    hex_, uni, unis, octal, other = m.groups()
    if hex_ or uni:
        return chr(int(hex_ or uni, 16))
    if unis:
        return "".join(chr(int(code, 16)) for code in unis.split())
    if octal:
        return chr(int(octal, 8))
    return _NAMED.get(other, other)


class _Scanner:
    """One left-to-right pass over a script: code, the literals in it, the forms skipped whole."""

    def __init__(self, src: str) -> None:
        self.src = src
        self.lines = [0, *(m.end() for m in re.finditer("\n", src))]
        self.heredocs: list[tuple[str, bool, int]] = []
        self.found: list[Literal] = []

    def place(self, at: int) -> tuple[int, int]:
        """The 1-based line and 0-based column of ``at``."""
        row = bisect_right(self.lines, at) - 1
        return row + 1, at - self.lines[row]

    def fail(self, what: str, at: int) -> UnreadableScript:
        return UnreadableScript(f"line {self.place(at)[0]}: {what}")

    def code(self, i: int, listed: bool, interp: bool) -> int:
        """Past the code from ``i``: to the end, or past the ``}`` closing an interpolation."""
        src, n, start = self.src, len(self.src), i
        stack: list[int] = []
        state, spaced = _BEG, False
        while i < n:
            c, nxt = src[i], src[i + 1 : i + 2]
            if c == "\n":
                i, state, spaced = self.newline(i + 1, interp), _BEG, False
                continue
            if c in " \t\r\f\v\\":
                i, spaced = i + 1, True
                continue
            # ponytail: whether `/ % << ?` open a regexp, %-literal, heredoc or character
            # literal is guessed from the token before them, not parsed. A local variable
            # written `a /b` or `a <<B`, `def /`, and a method given `?"` misread; most leave
            # something open and fail as UnreadableScript (a regexp must close on its line).
            # Known silent case: `cnt /2 + "q".size /2` reads `/2 + "q".size /` as a regexp
            # and drops "q". Task 12 measures this on real scripts; Ruby's lexer states with
            # local-variable tracking are the upgrade.
            opens = state == _BEG or (state == _ARG and spaced and nxt not in " \t\r\n=")
            if c in "\"'":
                i, state = self.string(i, listed), _END
            elif c == "#":
                i = _line_end(src, i)
            elif c == "/" and opens:
                i, state = self.regexp(i), _END
            elif c == "%" and opens and (m := _PERCENT.match(src, i)):
                i, state = self.delimited(m.end(), m.group(1) or m.group(2), i), _END
            elif c == "<" and opens and (m := _HEREDOC.match(src, i)):
                self.heredocs.append((m.group(2) or m.group(4), bool(m.group(1)), i))
                i, state = m.end(), _END
            elif c == "?" and opens and nxt.strip() and not (state == _ARG and nxt in "\"'"):
                i, state = i + (3 if nxt == "\\" else 2), _END
            elif c.isalpha() or c == "_":
                m = _WORD.match(src, i)
                i, state = m.end(), _BEG if m.group() in _BEGIN_WORDS else _ARG
            elif "0" <= c <= "9":
                i, state = _NUMBER.match(src, i).end(), _END
            elif c in "@$" and (m := _SIGIL.match(src, i)):
                i, state = m.end(), _END
            elif c in "([{":
                stack.append(i)
                i, state = i + 1, _BEG
            elif c in ")]}":
                if not stack and c == "}" and interp:
                    return i + 1
                if not stack or _PAIRS[src[stack.pop()]] != c:
                    raise self.fail("the brackets do not balance", i)
                i, state = i + 1, _END
            else:
                i, state = i + 1, _BEG
            spaced = False
        if interp:
            raise self.fail("the interpolation is not closed", start)
        if stack:
            raise self.fail("the brackets do not balance", stack[-1])
        if self.heredocs:
            raise self.fail("the heredoc is not closed", self.heredocs[0][2])
        return i

    def newline(self, i: int, interp: bool) -> int:
        """Past the heredoc bodies the ended line opened, then a ``=begin`` block or ``__END__``."""
        src, n = self.src, len(self.src)
        for ident, indented, at in self.heredocs:
            while True:
                if i >= n:
                    raise self.fail("the heredoc is not closed", at)
                end = _line_end(src, i)
                line, i = src[i:end], end + 1
                if (line.strip() if indented else line.rstrip("\r")) == ident:
                    break
        self.heredocs.clear()
        return min(i, n) if interp else self.line_start(min(i, n))

    def line_start(self, i: int) -> int:
        """Past a ``=begin`` block starting at ``i``; the end of the script at ``__END__``."""
        src = self.src
        if _DATA.match(src, i):
            return len(src)
        if _BLOCK.match(src, i):
            m = _BLOCK_END.search(src, i)
            if not m:
                raise self.fail("the =begin comment is not closed", i)
            return _line_end(src, m.start())
        return i

    def string(self, i: int, listed: bool) -> int:
        """Past the literal opening at ``i``, listing it when ``listed``."""
        src, quote = self.src, self.src[i]
        special = _SPECIAL[quote]
        parts: list[str] = []
        j = i + 1
        while True:
            m = special.search(src, j)
            if not m:
                raise self.fail("the string literal is not closed", i)
            k = m.start()
            parts.append(src[j:k].replace("\r\n", "\n"))
            c = src[k]
            if c == quote:
                break
            if c == "#" and (var := _DVAR.match(src, k + 1)):
                j = var.end()
                parts.append(f"#{{{var.group()}}}")
            elif c == "#":
                j = self.code(k + 2, False, True) if src.startswith("#{", k) else k + 1
                parts.append(src[k:j].replace("\r\n", "\n"))
            elif quote == "'":
                escaped = src[k + 1 : k + 2] in ("\\", "'")
                parts.append(src[k + 1] if escaped else "\\")
                j = k + 2 if escaped else k + 1
            elif e := _ESCAPE.match(src, k):
                parts.append(_unescape(e))
                j = e.end()
            else:
                raise self.fail("the string literal is not closed", i)
        if listed:
            text = "".join(parts)
            self.found.append(Literal(i, k + 1, quote, src[i + 1 : k], text, *self.place(i)))
        return k + 1

    def regexp(self, i: int) -> int:
        """Past the ``/.../`` regexp opening at ``i`` and its flags."""
        src, j = self.src, i + 1
        while j < len(src) and src[j] != "\n":
            if src[j] == "/":
                j += 1
                while j < len(src) and src[j] in "imxounse":
                    j += 1
                return j
            j += 2 if src[j] == "\\" else 1
        raise self.fail("the regexp is not closed", i)

    def delimited(self, i: int, opener: str, at: int) -> int:
        """Past the ``%`` literal opened at ``at`` whose body starts at ``i``."""
        src, closer, depth = self.src, _PAIRS.get(opener, opener), 0
        while i < len(src):
            c = src[i]
            if c == "\\":
                i += 2
                continue
            if c == closer:
                if depth == 0:
                    return i + 1
                depth -= 1
            elif c == opener:
                depth += 1
            i += 1
        raise self.fail("the %-literal is not closed", at)


def literals(source: str) -> list[Literal]:
    """The quoted literals of a script outside comments and skipped forms, before ``__END__``."""
    scanner = _Scanner(source)
    scanner.code(scanner.line_start(0), True, False)
    return scanner.found


def _pieces(text: str) -> Iterator[tuple[bool, str]]:
    """``text`` cut into plain runs and the ``#{...}`` that close, each flagged as code or not."""
    i = 0
    while (k := text.find("#{", i)) >= 0:
        try:
            end = _Scanner(text).code(k + 2, False, True)
        except UnreadableScript:
            yield False, text[i : k + 2]
            i = k + 2
            continue
        yield False, text[i:k]
        yield True, text[k:end]
        i = end
    yield False, text[i:]


def _shown(text: str) -> str:
    """``text`` without its ``#{...}`` and format specifiers."""
    return "".join(_SPEC.sub("", piece) for code, piece in _pieces(text) if not code)


def _escape(m: re.Match[str]) -> str:
    c = m.group()
    if c in '\\"#':
        return "\\" + c
    return {"\n": "\\n", "\t": "\\t"}.get(c) or f"\\x{ord(c):02X}"


def encode(text: str, quote: str) -> str:
    """The raw form between ``quote`` marks that :func:`literals` reads back as ``text``."""
    if quote == "'":
        return text.replace("\\", "\\\\").replace("'", "\\'")
    return "".join(piece if code else _UNSAFE.sub(_escape, piece) for code, piece in _pieces(text))


def placeholders(text: str) -> Counter[str]:
    """The ``#{...}``, format specifiers and newlines (counted as ``"\\n"``) of ``text``."""
    found: Counter[str] = Counter()
    for code, piece in _pieces(text):
        found.update([piece] if code else [*_SPEC.findall(piece), *["\\n"] * piece.count("\n")])
    return found


def placeholder_mismatch(source: str, target: str) -> tuple[list[str], list[str]]:
    """The placeholders ``target`` drops and adds; specifiers count only when ``source`` has one."""
    want, have = placeholders(source), placeholders(target)
    if any(key[0] == "%" for key in want):
        have["%"] += _shown(target).count("%")
    else:
        have = Counter({key: count for key, count in have.items() if key[0] != "%"})
    missing = list((want - have).elements())
    return missing, [key for key in (have - want).elements() if key != "\\n"]


def _around(source: str, literal: Literal) -> tuple[str, str]:
    """The code on the literal's line before its opening quote and after its closing quote."""
    line_start = source.rfind("\n", 0, literal.start) + 1
    return source[line_start : literal.start], source[literal.end : _line_end(source, literal.end)]


def compared(source: str, literal: Literal) -> bool:
    """Whether the code around this literal compares its text (rule 2)."""
    before, after = _around(source, literal)
    return bool(_COMPARED_BEFORE.search(before) or _COMPARED_AFTER.match(after))


def rule(source: str, literal: Literal, keys: frozenset[str], assets: frozenset[str]) -> Rule:
    """Rules 1-5 for one literal; ``keys`` are compared texts, ``assets`` file names unsuffixed."""
    text = literal.text
    before, after = _around(source, literal)
    if (
        "\\#{" in literal.raw
        or not any(c.isalpha() for c in _shown(text))
        or _PATH.search(text)
        or _BINARY.search(text)
        or (len(text) == 1 and (_TABLE_BEFORE.search(before) or _TABLE_AFTER.match(after)))
    ):
        return Rule.SKIP
    if text in keys:
        return Rule.KEY
    line_start = source.rfind("\n", 0, literal.start) + 1
    line = source[line_start : _line_end(source, literal.start)]
    if _CODE.search(line) or text in assets:
        return Rule.CODE
    if _DISPLAY.search(line) or (_CONSTANT.match(line) and "module Vocab" in source):
        return Rule.DISPLAY
    return Rule.OTHER
