"""Quality findings carry a code and values, and the interface tells them from those."""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

import bokucho
from bokucho.brackets import bracket_findings
from bokucho.findings import TEMPLATES, Finding
from bokucho.model import Context, TextUnit
from bokucho.qa import Issue, check_unit, format_problems, format_warnings
from bokucho.webapp import i18n

ASSETS = Path(bokucho.__file__).parent / "web" / "assets"


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def _bracket_findings():
    units = [
        TextUnit(id="u1", source="「やめて」", target="（住手）"),
        TextUnit(id="u2", source="「待って」", target="等一下"),
        TextUnit(id="u3", source="やあ", target="「你好」"),
    ]
    return [
        finding
        for unit in units
        for found in bracket_findings([unit], "")
        for _, finding in found.findings
    ]


def _findings() -> list[Finding]:
    unit = TextUnit(
        id="u", source="一行\n二行", target="很長的一行", context=Context(max_length=3)
    )
    return [
        *format_problems("%a% と %a%", "%a%"),
        *format_problems("<VAR 【名】>を", "<VAR 【名】><VAR 【他】>把", engine="livemaker"),
        *format_problems("<VAR 【名】>を", "<VAR 【他】>把", engine="livemaker"),
        *format_problems("おはよう", "おはよう", reject_echo=True),
        *format_problems("こんにちは", "你好은", target_lang="zh-Hant-TW"),
        *format_problems("を使った！", "！", target_lang="zh-Hant-TW"),
        *check_unit(unit, hans_convert=lambda text: text.replace("很", "狠")),
        *format_warnings("一行\n二行", "合併"),
        *_bracket_findings(),
    ]


def test_every_producer_names_a_known_code_and_hands_over_what_its_sentence_needs():
    findings = _findings()

    assert {finding.code for finding in findings} == {
        "variables_missing",
        "variables_unexpected",
        "variables_both",
        "echo",
        "language",
        "dropped",
        "length",
        "hans_residue",
        "line_count",
        "brackets_speech_to_thought_fix",
        "brackets_missing_or_extra",
        "brackets_missing_or_extra_fix",
    }
    for finding in findings:
        assert set(finding.params) == _placeholders(TEMPLATES[finding.code]), finding.code


def test_the_english_message_stays_what_the_cli_has_always_printed():
    messages = {finding.code: finding.message for finding in _findings()}

    assert messages["variables_missing"] == "missing variables: %a%"
    assert messages["variables_both"] == (
        "missing variables: <VAR 【名】>; unexpected variables: <VAR 【他】>"
    )
    assert messages["line_count"] == "line count changed: 2 -> 1"
    assert messages["language"] == "not written in zh-Hant-TW: 은"
    assert messages["length"] == "line of 5 chars exceeds max_length 3"
    assert messages["brackets_speech_to_thought_fix"] == (
        "speech 「」 turned into thought （）: 「」 -> （）; fix: 「住手」"
    )
    assert messages["brackets_missing_or_extra"] == "brackets lost or added: none -> 「」"
    assert messages["brackets_missing_or_extra_fix"] == (
        "brackets lost or added: 「」 -> none; fix: 「等一下」"
    )


def test_an_issue_keeps_the_code_and_values_beside_the_message():
    (finding,) = format_warnings("一行\n二行", "合併")

    issue = Issue.of("a.lsb", "u1", finding, severity="warning")

    assert (issue.check, issue.code, issue.params) == (
        "line-count",
        "line_count",
        {"source": 2, "target": 1},
    )
    assert issue.message == "line count changed: 2 -> 1"


def test_the_frontend_has_a_template_for_each_code_worded_as_the_backend_words_it():
    source = (ASSETS / "qa_messages.js").read_text(encoding="utf-8")
    body = re.search(r"QA_MESSAGES = \{(.*?)\n\};", source, re.S)

    table = dict(re.findall(r'^\s*(\w+): "((?:[^"\\]|\\.)*)",?$', body.group(1), re.M))

    assert table == TEMPLATES


@pytest.mark.parametrize("locale", ["en", "zh_TW"])
def test_each_catalog_words_every_finding(locale):
    catalog = i18n.load_catalog(locale)["QaIssue"]

    assert set(TEMPLATES.values()) | {"none"} <= set(catalog)
    for template in TEMPLATES.values():
        assert _placeholders(catalog[template]) == _placeholders(template), template


def test_the_traditional_chinese_catalog_does_not_hand_back_the_english():
    catalog = i18n.load_catalog("zh_TW")["QaIssue"]

    assert [template for template in TEMPLATES.values() if catalog[template] == template] == []
