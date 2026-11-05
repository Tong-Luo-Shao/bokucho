from __future__ import annotations

from pathlib import Path

from bokucho.application.glossary import (
    delete_glossary,
    glossary_occurrences,
    upsert_glossary,
)
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.application.text import effective_glossary
from bokucho.model import StringFile, TextUnit
from bokucho.translate.glossary import Glossary
from bokucho.translators.base import TranslationContext, build_prompt


def test_upsert_keeps_one_entry_per_source(tmp_path: Path) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path, "test", "ja", "zh-Hant", "livemaker")
    )

    assert upsert_glossary(project, "hero", "勇者", "name") is True
    assert upsert_glossary(project, "hero", "英雄", None) is False

    entries = Glossary.load(project.glossary_path).entries
    assert [(entry.source, entry.target, entry.note) for entry in entries] == [
        ("hero", "英雄", "name")
    ]


def test_delete_returns_false_when_absent(tmp_path: Path) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path, "test", "ja", "zh-Hant", "livemaker")
    )

    assert delete_glossary(project, "missing") is False
    upsert_glossary(project, "hero", "英雄")
    assert delete_glossary(project, "hero") is True
    assert Glossary.load(project.glossary_path).entries == []


def test_occurrences_count_source_and_target_substrings(tmp_path: Path) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path, "test", "ja", "zh-Hant", "livemaker")
    )
    upsert_glossary(project, "hero", "英雄")
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[TextUnit(id="u1", source="hero meets hero", target="英雄")],
        ),
        "a.lsb.json",
    )

    assert glossary_occurrences(project) == {"hero": 2}


def test_occurrences_never_match_across_two_lines(tmp_path: Path) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path, "test", "ja", "zh-Hant", "livemaker")
    )
    upsert_glossary(project, "魔王", "Demon King")
    upsert_glossary(project, "王城", "castle")
    project.save_string_file(
        StringFile(
            source_file="a.lsb",
            units=[
                TextUnit(id="u1", source="魔", target="魔王"),
                TextUnit(id="u2", source="王城", target=""),
            ],
        ),
        "a.lsb.json",
    )

    assert glossary_occurrences(project) == {"魔王": 1, "王城": 1}


def test_every_term_is_sent_as_guidance_even_one_saved_as_a_rule(tmp_path: Path) -> None:
    project = create_project(
        CreateProjectRequest(tmp_path, "test", "ja", "zh-Hant", "livemaker")
    )
    project.glossary_path.write_text(
        '{"entries": [{"source": "アオイ", "target": "阿奧伊", "force": true}]}',
        encoding="utf-8",
    )
    context = TranslationContext(source_lang="ja", target_lang="zh-Hant")
    unit = TextUnit(id="u", source="アオイは笑った。")

    system, _ = build_prompt([unit], context, effective_glossary(project))

    assert "- Use this glossary for names and terms:\n  アオイ => 阿奧伊" in system
    assert "must be used exactly" not in system
