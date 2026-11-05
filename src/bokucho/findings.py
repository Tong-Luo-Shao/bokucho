"""Quality findings: a stable code and the values that fill its English sentence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

_BRACKETS = {
    "brackets_speech_to_thought": "speech 「」 turned into thought （）",
    "brackets_thought_to_speech": "thought （） turned into speech 「」",
    "brackets_mixed_swap": "bracket kinds changed",
    "brackets_missing_or_extra": "brackets lost or added",
    "brackets_swapped": "reads as the translation of a neighbouring line",
}

TEMPLATES: dict[str, str] = {
    "variables_missing": "missing variables: {missing}",
    "variables_unexpected": "unexpected variables: {unexpected}",
    "variables_both": "missing variables: {missing}; unexpected variables: {unexpected}",
    "echo": "target is the source, handed back untranslated",
    "language": "not written in {language}: {found}",
    "dropped": "words dropped from: {lines}",
    "length": "line of {longest} chars exceeds max_length {limit}",
    "hans_residue": "still converts: {gist}",
    "line_count": "line count changed: {source} -> {target}",
    **{code: f"{said}: {{source}} -> {{target}}" for code, said in _BRACKETS.items()},
    **{
        f"{code}_fix": f"{said}: {{source}} -> {{target}}; fix: {{fixed}}"
        for code, said in _BRACKETS.items()
    },
    "values_search_any_found": (
        "{where} searches {value} for any of {words}: found, "
        "but none of {forms} is in {form}"
    ),
    "values_search_any_missing": (
        "{where} searches {value} for any of {words}: not found, "
        "but one of {forms} is in {form}"
    ),
    "values_search_found": (
        "{where} searches {value} for {word}: found, but none of {forms} is in {form}"
    ),
    "values_search_missing": (
        "{where} searches {value} for {word}: not found, but one of {forms} is in {form}"
    ),
    "values_cut": "{where} cuts {value} before {word} into {part}, but {form} into {got}",
    "values_replace": (
        "{where} replaces {old} with {new} in {value} to get {result}, but {form} gives {got}"
    ),
    "values_join": "{where} joins {pieces} into {whole}, but the translations into {game}",
    "script_key": "a script compares {source}: shipped here as {here}, as {elsewhere} at {where}",
    "script_asset": "{source} names a game file: {file}",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing a quality check found, before it is pinned to a unit."""

    check: str
    code: str
    params: Mapping[str, str | int | None] = field(default_factory=dict)

    @property
    def message(self) -> str:
        spelled = {key: "none" if value is None else value for key, value in self.params.items()}
        return TEMPLATES[self.code].format(**spelled)
