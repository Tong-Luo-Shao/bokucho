"""RED tests for the project lifecycle bridge methods: recent, create, open."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bokucho.application.text import translate_project
from bokucho.model import StringFile, TextUnit
from bokucho.project import Project
from bokucho.translators.base import TranslationContext, build_prompt
from bokucho.webapp.api import Api


def test_recent_projects_lists_engine_and_last_opened(api: Api, app_state, tmp_path: Path) -> None:
    destination = tmp_path / "alpha"
    created = api.create_project(
        name="alpha",
        engine="livemaker",
        destination=str(destination),
        source_lang="ja",
        target_lang="zh-Hant",
    )
    assert "error" not in created

    recent = api.recent_projects()

    assert json.loads(json.dumps(recent)) == recent
    assert len(recent) == 1
    entry = recent[0]
    assert Path(entry["path"]) == Path(created["root"]).resolve()
    assert entry["engine"] == "livemaker"
    assert entry["last_opened"]


def test_create_project_with_game_folder_auto_detects_engine(api: Api, tmp_path: Path) -> None:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    (game_dir / "live.lpb").write_bytes(b"")
    destination = tmp_path / "proj"

    result = api.create_project(
        name="Web",
        engine=None,
        destination=str(destination),
        source_lang="ja",
        target_lang="zh-Hant",
        game=str(game_dir),
    )

    assert "error" not in result
    project = Project.load(result["root"])
    assert project.config.engine.type == "livemaker"
    assert Path(project.config.engine.game_path) == game_dir.resolve()


@pytest.mark.parametrize(("marker", "expected"), [
    ("www/data/System.json", "rpgmaker-mv"),
    ("data/System.json", "rpgmaker-mz"),
    ("Data/System.rvdata2", "rpgmaker-vx-ace"),
])
def test_create_project_persists_concrete_rpgmaker_engine(
    api: Api, tmp_path: Path, marker: str, expected: str,
) -> None:
    game_dir = tmp_path / "game"
    game_file = game_dir / marker
    game_file.parent.mkdir(parents=True)
    game_file.write_bytes(b"")

    result = api.create_project(
        name="Web",
        engine=None,
        destination=str(tmp_path / "proj"),
        source_lang="ja",
        target_lang="zh-Hant",
        game=str(game_dir),
    )

    assert result["engine"] == expected
    assert Project.load(result["root"]).config.engine.type == expected


def test_open_project_returns_overview_summary(api: Api, tmp_path: Path) -> None:
    destination = tmp_path / "proj"
    created = api.create_project(
        name="Proj",
        engine="livemaker",
        destination=str(destination),
        source_lang="ja",
        target_lang="zh-Hant",
    )
    assert "error" not in created
    project = Project.load(created["root"])
    project.save_string_file(
        StringFile(source_file="scene1", units=[TextUnit(id="1", source="hello")]),
        "scene1.json",
    )

    summary = api.open_project(created["root"])

    assert "error" not in summary
    assert (summary["name"], summary["engine"]) == ("Proj", "livemaker")
    assert api.unit_counts()["status_counts"]["untranslated"] == 1


@pytest.mark.parametrize("source_lang,target_lang,answer", [
    ("ja", "en", "Hello"),
    ("und", "ko", "안녕하세요"),
])
def test_saved_translation_languages_reach_the_provider_after_reload(
    api: Api, new_project, source_lang: str, target_lang: str, answer: str,
) -> None:
    project = new_project()
    project.save_config(project.config.model_copy(update={
        "translator": project.config.translator.model_copy(update={"max_request_tokens": 0}),
    }))
    project.save_string_file(
        StringFile(source_file="scene", units=[TextUnit(id="1", source="こんにちは")]),
        "scene.json",
    )
    assert "error" not in api.open_project(str(project.root))
    saved = api.save_prompt_preset({
        "id": "user:languages",
        "name": "Languages",
        "system": "From {Orig_Lang} to {Target_Lang}; legacy {source_lang}/{target_lang}.",
        "user": "{entries_json}",
    })
    assert "error" not in saved
    assert "error" not in api.select_prompt_preset("user:languages")
    assert "error" not in api.save_project_settings(
        source_lang=source_lang, target_lang=target_lang,
    )
    reloaded = Project.load(project.root)
    assert (reloaded.config.source_lang, reloaded.config.target_lang) == (source_lang, target_lang)
    assert "error" not in api.open_project(str(project.root))
    assert api.get_settings()["target_lang"] == target_lang
    sent = []

    class Provider:
        def translate(self, batch: list[TextUnit], context: TranslationContext, glossary=None):
            sent.append(build_prompt(batch, context, glossary)[0])
            return [answer for _ in batch]

    result = translate_project(reloaded, Provider(), sleep=lambda _: None)

    assert result.translated == 1
    assert f"From {source_lang} to {target_lang}; legacy {source_lang}/{target_lang}." in sent[0]
    assert "BCP 47" in sent[0]
    assert ("Detect the source language of each entry" in sent[0]) == (source_lang == "und")
    assert Project.load(project.root).load_string_files()[0].units[0].target == answer
