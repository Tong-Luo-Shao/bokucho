"""RED/GREEN tests for the glossary bridge methods."""

from __future__ import annotations

from pathlib import Path

from bokucho.application.projects import string_filename
from bokucho.model import StringFile, TextUnit
from bokucho.project import Project
from bokucho.translate.glossary import Glossary, GlossaryEntry, global_glossary_path
from bokucho.webapp.api import Api


def _seed_project(api: Api, tmp_path: Path, name: str) -> Project:
    destination = tmp_path / name
    created = api.create_project(
        name=name,
        engine="livemaker",
        destination=str(destination),
        source_lang="ja",
        target_lang="zh-Hant",
    )
    assert "error" not in created
    return Project.load(created["root"])


def test_glossary_entries_lists_project_and_global_with_origin(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(api, tmp_path, "glossary")
    Glossary(entries=[GlossaryEntry(source="勇者", target="hero")]).save(project.glossary_path)
    Glossary(entries=[GlossaryEntry(source="魔王", target="demon lord")]).save(
        global_glossary_path()
    )
    assert "error" not in api.open_project(str(project.root))

    entries = api.glossary_entries()

    by_source = {entry["source"]: entry for entry in entries}
    assert by_source["勇者"]["origin"] == "project"
    assert by_source["勇者"]["target"] == "hero"
    assert by_source["魔王"]["origin"] == "global"
    assert by_source["魔王"]["target"] == "demon lord"


def test_save_glossary_entry_with_global_scope_writes_the_user_level_file(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(api, tmp_path, "global-save")
    assert "error" not in api.open_project(str(project.root))

    result = api.save_glossary_entry("光", "light", scope="global")

    assert "error" not in result
    global_entries = Glossary.load(global_glossary_path()).entries
    assert any(e.source == "光" and e.target == "light" for e in global_entries)
    project_entries = Glossary.load(project.glossary_path).entries
    assert not any(e.source == "光" for e in project_entries)


def test_glossary_mutations_refresh_suggestions_and_spans_on_the_next_call(
    api: Api, tmp_path: Path
) -> None:
    project = _seed_project(api, tmp_path, "refresh")
    project.save_string_file(
        StringFile(source_file="a.lsb", units=[TextUnit(id="u1", source="旅人へ")]),
        string_filename("a.lsb"),
    )
    assert "error" not in api.open_project(str(project.root))

    before = api.unit_detail("a.lsb", "u1")
    assert not any(s["origin"] == "glossary" for s in before["suggestions"])
    assert before["spans"] == []

    assert "error" not in api.save_glossary_entry("旅人", "traveler", scope="project")

    after_save = api.unit_detail("a.lsb", "u1")
    assert any(
        s["origin"] == "glossary" and s["target"] == "traveler"
        for s in after_save["suggestions"]
    )
    assert after_save["spans"] == [[0, 2, "traveler"]]

    assert "error" not in api.delete_glossary_entry("旅人")

    after_delete = api.unit_detail("a.lsb", "u1")
    assert not any(s["origin"] == "glossary" for s in after_delete["suggestions"])
    assert after_delete["spans"] == []



def test_deleting_a_global_row_leaves_the_project_term_alone(api: Api, tmp_path: Path) -> None:
    project = _seed_project(api, tmp_path, "delete-global")
    Glossary(entries=[GlossaryEntry(source="勇者", target="hero")]).save(project.glossary_path)
    Glossary(
        entries=[
            GlossaryEntry(source="勇者", target="brave"),
            GlossaryEntry(source="魔王", target="demon lord"),
        ]
    ).save(global_glossary_path())
    assert "error" not in api.open_project(str(project.root))

    assert api.delete_glossary_entry("勇者", scope="global") == {"deleted": True}
    assert api.delete_glossary_entry("魔王", scope="global") == {"deleted": True}

    assert [(e["source"], e["origin"]) for e in api.glossary_entries()] == [("勇者", "project")]


def test_glossary_entries_carry_no_force_flag(api: Api, tmp_path: Path) -> None:
    project = _seed_project(api, tmp_path, "no-force")
    Glossary(entries=[GlossaryEntry(source="勇者", target="hero")]).save(project.glossary_path)
    assert "error" not in api.open_project(str(project.root))

    [entry] = [e for e in api.glossary_entries() if e["source"] == "勇者"]

    assert "force" not in entry
