from __future__ import annotations

import json

import pytest

from bokucho.application.quality import qa_report
from bokucho.application.text import recheck_drafts, translate_project
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.translators.base import build_prompt


@pytest.mark.parametrize("kind", ["text", "image"])
def test_only_source_language_lines_are_sent_and_other_lines_stay_in_place(
    new_project, kind: str,
) -> None:
    project = new_project(source_lang="ja", target_lang="zh-Hant-TW")
    project.save_config(project.config.model_copy(update={
        "translator": project.config.translator.model_copy(update={"max_request_tokens": 0}),
    }))
    project = Project.load(project.root)
    source = "  Hello world  \n\n　へっぉをおるど\n这里是简体中文"
    project.save_string_file(StringFile(source_file="scene", units=[
        TextUnit(id="english", source="Hello world"),
        TextUnit(id="mixed", source=source, kind=kind),
    ]), "scene.json")
    sent = []

    class Provider:
        def translate(self, batch, context, glossary=None):
            sent.append(build_prompt(batch, context, glossary))
            return ["哈囉世界" for _ in batch]

    result = translate_project(project, Provider(), limit=1, sleep=lambda _: None)

    assert result.total == result.translated == 1
    assert result.failed == 0
    assert len(sent) == 1
    assert json.loads(sent[0][1])[0]["text"] == ["へっぉをおるど"]
    assert "Hello world" not in sent[0][1]
    assert "这里是简体中文" not in sent[0][1]
    saved = Project.load(project.root).load_string_files()[0].units
    assert saved[0].status is Status.UNTRANSLATED
    assert saved[0].target == ""
    assert saved[1].source == source
    assert saved[1].target == "  Hello world  \n\n　哈囉世界\n这里是简体中文"
    assert saved[1].status is Status.MT
    assert not qa_report(project, project.load_string_files()).issues
    before = saved[1].target
    recheck_drafts(project)
    assert Project.load(project.root).load_string_files()[0].units[1].target == before


def test_a_run_with_no_matching_language_leaves_units_untranslated(new_project) -> None:
    project = new_project(source_lang="ja")
    project.save_config(project.config.model_copy(update={
        "translator": project.config.translator.model_copy(update={"max_request_tokens": 0}),
    }))
    project = Project.load(project.root)
    project.save_string_file(StringFile(source_file="scene", units=[
        TextUnit(id="english", source="Hello world"),
        TextUnit(id="unknown", source="https://example.com"),
    ]), "scene.json")

    class Provider:
        def translate(self, *_args, **_kwargs):
            pytest.fail("Non-source text must never reach the provider")

    result = translate_project(project, Provider())

    assert result.total == result.processed == result.batches == 0
    for unit in Project.load(project.root).load_string_files()[0].units:
        assert unit.status is Status.UNTRANSLATED
        assert unit.target == ""


@pytest.mark.parametrize("source_lang,chosen,other", [
    ("zh-cn", "这里是简体中文，这个游戏让我们学习语言并开始新的冒险。",
     "這是一段繁體中文的文字，歡迎來到臺灣，我們一起學習語言和進行遊戲。"),
    ("zh-tw", "這是一段繁體中文的文字，歡迎來到臺灣，我們一起學習語言和進行遊戲。",
     "这里是简体中文，这个游戏让我们学习语言并开始新的冒险。"),
])
def test_region_source_tags_send_only_the_selected_language(
    new_project, source_lang, chosen, other,
):
    project = new_project(source_lang=source_lang, target_lang="en")
    project.save_config(project.config.model_copy(update={
        "translator": project.config.translator.model_copy(update={"max_request_tokens": 0}),
    }))
    project = Project.load(project.root)
    project.save_string_file(StringFile(source_file="scene", units=[
        TextUnit(id="chosen", source=chosen), TextUnit(id="other", source=other),
    ]), "scene.json")
    sent = []

    class Provider:
        def translate(self, batch, context, glossary=None):
            sent.extend(unit.source for unit in batch)
            return ["An English translation." for _ in batch]

    result = translate_project(project, Provider(), sleep=lambda _: None)
    assert result.total == result.translated == 1
    assert sent == [chosen]
    saved = {unit.id: unit for unit in Project.load(project.root).load_string_files()[0].units}
    assert saved["other"].source == other
    assert saved["other"].target == ""
    assert saved["other"].status is Status.UNTRANSLATED
