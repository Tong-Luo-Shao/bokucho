"""QA checks over translated units: format tokens, length, line count."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from bokucho.engines import engine_family
from bokucho.engines.ruby_literals import placeholder_mismatch
from bokucho.findings import Finding
from bokucho.language import matches_source_language, normalize_language_tag
from bokucho.layout import TextLayout, layout_content, split_layout
from bokucho.model import TextUnit

# ponytail: [[ / {{ literal escapes can false-positive; noise-only, tighten if it bites.
_VAR_RE = re.compile(r"%(?:[A-Za-z_][A-Za-z0-9_.]*%|\d+)")

ENGINE_MARKUP: dict[str, tuple[re.Pattern[str], ...]] = {
    "renpy": (
        re.compile(r"\[[^\[\]]+\]"),
        re.compile(r"\{[^{}]+\}"),
    ),
    "livemaker": (re.compile(r"<VAR [^<>\n]+>"),),
}

_EXACT_MARKUP_ENGINES = frozenset({"livemaker"})

ENGINE_CODECS = {"livemaker": "cp932"}

ENGINE_CONTROL_CODES: dict[str, re.Pattern[str]] = {
    "rpgmaker": re.compile(r"\\[CVNPI]\[[^\]]*\]|\\[G{}$.|!><^]", re.IGNORECASE),
}


def source_layout(source: str, source_lang: str, engine: str = "") -> tuple[TextLayout, str]:
    """Keep non-source lines literal, testing words without runtime markup."""
    if not has_word(without_markup(source, engine)):
        return split_layout(source)
    return split_layout(
        source,
        include=lambda line: matches_source_language(without_markup(line, engine), source_lang),
    )


def translation_tokens(source: str, engine: str = "") -> list[dict[str, str | int]]:
    """Protected runtime tokens in source order, grouped with their occurrence count."""
    engine = engine_family(engine)
    matches: list[tuple[int, str, str]] = [
        (match.start(), match.group(), "variable")
        for match in _VAR_RE.finditer(source)
    ]
    for index, pattern in enumerate(ENGINE_MARKUP.get(engine, ())):
        kind = "variable" if index == 0 else "formatting"
        matches.extend(
            (match.start(), match.group(), kind)
            for match in pattern.finditer(source)
        )
    control_codes = ENGINE_CONTROL_CODES.get(engine)
    if control_codes is not None:
        for match in control_codes.finditer(source):
            code = match.group()
            kind = "variable" if code[:2].upper() in (r"\V", r"\N", r"\P") else "control"
            matches.append((match.start(), code, kind))

    counts: dict[tuple[str, str], int] = {}
    for _, token, kind in sorted(matches):
        key = (token, kind)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"token": token, "kind": kind, "count": count}
        for (token, kind), count in counts.items()
    ]


def missing_control_codes(source: str, target: str, engine: str) -> list[str]:
    """Control codes present in ``source`` that the translation dropped."""
    pattern = ENGINE_CONTROL_CODES.get(engine_family(engine))
    if pattern is None:
        return []
    remaining = target
    missing: list[str] = []
    for code in pattern.findall(source):
        if code in remaining:
            remaining = remaining.replace(code, "", 1)
        else:
            missing.append(code)
    return missing


@dataclass
class Issue:
    file: str
    unit_id: str
    check: str
    message: str
    severity: Literal["error", "warning"] = "error"
    code: str = ""
    params: dict[str, str | int | None] = field(default_factory=dict)

    @classmethod
    def of(
        cls,
        file: str,
        unit_id: str,
        finding: Finding,
        severity: Literal["error", "warning"] = "error",
    ) -> Issue:
        return cls(
            file,
            unit_id,
            finding.check,
            finding.message,
            severity,
            finding.code,
            dict(finding.params),
        )


_KANA_RE = re.compile(r"[ぁ-ゖゝゞァ-ヺヽ-ヿ]")

_HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7af]")

_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_CJK_LETTERS = (
    "CJK",
    "IDEOGRAPHIC",
    "BOPOMOFO",
    "HIRAGANA",
    "KATAKANA",
    "HALFWIDTH KATAKANA",
    "HANGUL",
    "HALFWIDTH HANGUL",
)


def kana_residue(target: str) -> str:
    """The kana left in ``target``, "" if it holds none."""
    return "".join(dict.fromkeys(_KANA_RE.findall(target)))


def _other_words(text: str) -> list[tuple[str, bool]]:
    """Runs of non-CJK letters, each with whether a Chinese character touches it."""
    text = unicodedata.normalize("NFKC", text)
    other = [
        character.isalpha()
        and not unicodedata.name(character, "").startswith(_CJK_LETTERS)
        for character in text
    ]
    words = []
    start = None
    for index, is_other in enumerate([*other, False]):
        if is_other and start is None:
            start = index
        elif not is_other and start is not None:
            if text[start - 1 : start] == "\\":
                words.append((text[start - 1 : index], False))
            else:
                glued = _is_hanzi(text, start - 1) or _is_hanzi(text, index)
                words.append((text[start:index], glued))
            start = None
    return words


def _is_hanzi(text: str, index: int) -> bool:
    return 0 <= index < len(text) and unicodedata.name(text[index], "").startswith("CJK")


def traditional_codec(target_lang: str) -> str | None:
    """The charset a Traditional target is written in, None for any other."""
    return {"zh-tw": "cp950", "zh-hk": "big5hkscs", "zh-mo": "big5hkscs"}.get(
        normalize_language_tag(target_lang)
    )


def foreign_text(source: str, target: str, target_lang: str) -> list[str]:
    """What in ``target`` is not written in ``target_lang``, [] if nothing."""
    language = target_lang.split("-")[0].lower()
    found = []
    if language != "ja" and (kana := kana_residue(target)):
        found.append(kana)
    if language != "ko" and (hangul := "".join(dict.fromkeys(_HANGUL_RE.findall(target)))):
        found.append(hangul)
    han = "".join(dict.fromkeys(_HAN_RE.findall(target)))
    if language not in ("zh", "ja", "ko") and han:
        found.append(han)
    if language in ("zh", "ja", "ko"):
        kept = {word.casefold() for word, _ in _other_words(source)}
        # ponytail: "glued, and one letter or capitals" is a heuristic read off
        # one game; a list of the Latin Chinese writes with if it misjudges.
        found.extend(
            dict.fromkeys(
                word
                for word, glued in _other_words(target)
                if word.casefold() not in kept
                and not (glued and (len(word) == 1 or word.isupper()))
                # ponytail: "capitalized = name" lets a stray English word in
                # title case through; a name list if one is ever saved that way.
                and not (word.isascii() and word[0].isupper() and not word.isupper())
            )
        )
    return found


_SOUNDS = str.maketrans("", "", "っッぁぃぅぇぉァィゥェォー")

_SENTENCE_ENDS = tuple("。．.！!？?」』）)】")


def dropped_lines(source: str, target: str) -> list[str]:
    """The source lines whose words the target's line for them does not carry."""
    sources = split_layout(source)[1].split("\n")
    targets = split_layout(target)[1].split("\n")
    if len(sources) != len(targets):
        return []
    return [
        line
        for index, (line, answer) in enumerate(zip(sources, targets, strict=True))
        if has_word(line.translate(_SOUNDS))
        and not has_word(answer)
        and not _runs_on(sources, index)
    ]


def _runs_on(lines: list[str], index: int) -> bool:
    """Whether line ``index`` continues a sentence left open by the line above."""
    above = lines[index - 1].rstrip() if index else ""
    return bool(above) and not above.endswith(_SENTENCE_ENDS)


def has_word(text: str) -> bool:
    """Whether ``text`` contains any letter."""
    return any(character.isalpha() for character in text)


def echoes_source(source: str, target: str) -> bool:
    """The target is the source handed back, ignoring layout; only kana sources count."""
    return bool(kana_residue(source)) and _squeezed(source) == _squeezed(target)


def _squeezed(text: str) -> str:
    """``text`` without whitespace."""
    return "".join(text.split()).replace("　", "")


def format_issues(
    source: str,
    target: str,
    reject_echo: bool = False,
    engine: str = "",
    target_lang: str = "",
    script: bool = False,
) -> list[str]:
    """Problem messages for the format checks, empty if none."""
    return [
        problem.message
        for problem in format_problems(source, target, reject_echo, engine, target_lang, script)
    ]


def format_problems(
    source: str,
    target: str,
    reject_echo: bool = False,
    engine: str = "",
    target_lang: str = "",
    script: bool = False,
) -> list[Finding]:
    """The format problems, empty if none; a script literal is held to Ruby's placeholders."""
    missing: list[str] = []
    extra: list[str] = []
    if script:
        missing, extra = placeholder_mismatch(source, target)
    else:
        exact = ENGINE_MARKUP.get(engine, ()) if engine in _EXACT_MARKUP_ENGINES else ()
        for regex in markup_patterns(engine):
            held = Counter(regex.findall(source))
            written = Counter(regex.findall(target))
            missing.extend((held - written).elements())
            if regex in exact:
                extra.extend((written - held).elements())
        missing.extend(missing_control_codes(source, target, engine))
    if missing or extra:
        params = {"missing": ", ".join(missing), "unexpected": ", ".join(extra)}
        if missing and extra:
            return [Finding("variables", "variables_both", params)]
        if missing:
            return [Finding("variables", "variables_missing", {"missing": params["missing"]})]
        return [Finding("variables", "variables_unexpected", {"unexpected": params["unexpected"]})]
    source = without_markup(source, engine)
    target = without_markup(target, engine)
    if reject_echo and echoes_source(source, target):
        return [Finding("echo", "echo")]
    if target_lang and (foreign := foreign_text(source, target, target_lang)):
        found = " ".join(foreign)
        return [Finding("language", "language", {"language": target_lang, "found": found})]
    if dropped := dropped_lines(source, target):
        return [Finding("dropped", "dropped", {"lines": " / ".join(dropped)})]
    return []


def problem_marks(
    source: str,
    target: str,
    reject_echo: bool = False,
    engine: str = "",
    target_lang: str = "",
    script: bool = False,
) -> list[str]:
    """The parts of ``target`` that :func:`format_problems` flags."""
    problems = format_problems(source, target, reject_echo, engine, target_lang, script)
    if not problems:
        return []
    check = problems[0].check
    if check == "variables":
        if script:
            return placeholder_mismatch(source, target)[1]
        exact = ENGINE_MARKUP.get(engine, ()) if engine in _EXACT_MARKUP_ENGINES else ()
        return [
            token
            for regex in exact
            for token in (Counter(regex.findall(target)) - Counter(regex.findall(source)))
        ]
    if check == "echo":
        return [target]
    if check == "language":
        found = foreign_text(
            without_markup(source, engine), without_markup(target, engine), target_lang
        )
        return [
            mark
            for item in found
            for mark in (
                list(item)
                if any(script.fullmatch(item[0]) for script in (_KANA_RE, _HANGUL_RE, _HAN_RE))
                else [item]
            )
        ]
    sources = split_layout(source)[1].split("\n")
    targets = split_layout(target)[1].split("\n")
    dropped = set(dropped_lines(source, target))
    return [
        answer
        for line, answer in zip(sources, targets, strict=False)
        if line in dropped and answer
    ]


def without_markup(text: str, engine: str) -> str:
    """``text`` with the engine's markup removed."""
    for regex in markup_patterns(engine):
        text = regex.sub("", text)
    return text


def around_markup(
    text: str,
    patterns: Sequence[re.Pattern[str]],
    convert: Callable[[str], str],
) -> str:
    """``text`` with ``convert`` applied only between ``patterns`` matches."""
    if not patterns:
        return convert(text)

    def converted(piece: str) -> str:
        return convert(piece) if piece else piece

    tokens = re.compile("|".join(f"(?:{regex.pattern})" for regex in patterns))
    pieces: list[str] = []
    cursor = 0
    for match in tokens.finditer(text):
        pieces.append(converted(text[cursor : match.start()]))
        pieces.append(match.group())
        cursor = match.end()
    pieces.append(converted(text[cursor:]))
    return "".join(pieces)


def markup_patterns(engine: str) -> tuple[re.Pattern[str], ...]:
    """Patterns for tokens a translation must keep under ``engine``."""
    return (_VAR_RE, *ENGINE_MARKUP.get(engine_family(engine), ()))


def format_warnings(source: str, target: str) -> list[Finding]:
    """Advisory formatting differences that never block review or release."""
    source_lines = len(source.splitlines())
    target_lines = len(target.splitlines())
    if source_lines != target_lines:
        return [
            Finding(
                "line-count", "line_count", {"source": source_lines, "target": target_lines}
            )
        ]
    return []


def check_unit(
    unit: TextUnit,
    hans_convert=None,
    reject_echo: bool = False,
    engine: str = "",
    target_lang: str = "",
    source_lang: str = "",
) -> list[Finding]:
    """All problems for one translated unit."""
    layout, source = source_layout(unit.source, source_lang, engine)
    target = layout_content(layout, unit.target)
    if target is None:
        source, target = unit.source, unit.target
    problems = format_problems(
        source, target, reject_echo, engine, target_lang, script=unit.kind == "script"
    )

    max_length = unit.context.max_length
    if max_length:
        longest = max(len(line) for line in unit.target.splitlines() or [""])
        if longest > max_length:
            problems.append(
                Finding("length", "length", {"longest": longest, "limit": max_length})
            )

    if hans_convert:
        converted = around_markup(target, markup_patterns(engine), hans_convert)
        if converted != target:
            gist = _conversion_gist(target, converted)
            problems.append(Finding("hans-residue", "hans_residue", {"gist": gist}))

    return problems


def _conversion_gist(target: str, converted: str, window: int = 12) -> str:
    """The part of ``target`` the converter would rewrite, and what into."""
    if len(target) == len(converted):
        changed = sorted({a for a, b in zip(target, converted, strict=True) if a != b})
        return "".join(changed[:10]) + ("…" if len(changed) > 10 else "")
    start = next(
        (i for i, (a, b) in enumerate(zip(target, converted, strict=False)) if a != b),
        min(len(target), len(converted)),
    )
    return f"{target[start : start + window]} -> {converted[start : start + window]}"


def encodable(ch: str, codec: str) -> bool:
    try:
        ch.encode(codec)
        return True
    except UnicodeEncodeError:
        return False
