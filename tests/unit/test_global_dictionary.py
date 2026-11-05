"""Tests for the global dictionary (user-level glossary + terms)."""

import logging

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bokucho import cli
from bokucho.application.text import translate_project
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.translate.glossary import (
    Glossary,
    GlossaryEntry,
    global_dir,
    global_glossary_path,
    global_terms_path,
    load_global_glossary,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "bokucho-home"
    monkeypatch.setenv("BOKUCHO_HOME", str(path))
    return path


def test_global_dir_env_override(home):
    assert global_dir() == home
    assert global_glossary_path() == home / "glossary.json"
    assert global_terms_path() == home / "terms.tsv"


def test_load_global_glossary_missing_is_empty(home):
    assert load_global_glossary().entries == []


def test_load_global_glossary_corrupt_raises(home):
    home.mkdir(parents=True)
    (home / "glossary.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_global_glossary()


runner = CliRunner()


def _project(tmp_path, units):
    proj_dir = tmp_path / "proj"
    assert runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"]).exit_code == 0
    project = Project.load(proj_dir)
    project.save_string_file(StringFile(source_file="a.lsb", units=units), "a.lsb.json")
    return _size_by_count(project)


def _size_by_count(project: Project) -> Project:
    """Batch by unit count, so these tests need no token counter."""
    translator = project.config.translator
    project.save_config(
        project.config.model_copy(
            update={
                "translator": translator.model_copy(
                    update={
                        "max_request_tokens": 0,
                        "interfaces": [
                            i.model_copy(update={"max_request_tokens": 0})
                            for i in translator.interfaces
                        ],
                    }
                )
            }
        )
    )
    return Project.load(project.root)


class FakeTranslator:
    def __init__(self):
        self.glossary = None

    def translate(self, batch, context, glossary=None):
        self.glossary = dict(glossary or {})
        return ["譯文" for _ in batch]


def test_translate_merges_global_glossary_project_wins(home, tmp_path):
    home.mkdir(parents=True)
    Glossary(entries=[GlossaryEntry(source="ヒロイン", target="全域女主"),
                      GlossaryEntry(source="魔王", target="魔王")]).save(home / "glossary.json")
    project = _project(tmp_path, [TextUnit(id="u1", source="ヒロインと魔王")])
    runner.invoke(cli.app, ["glossary", "add", "ヒロイン", "專案女主",
                            "--project", str(project.root)])

    fake = FakeTranslator()
    translate_project(project, fake)
    assert fake.glossary == {"ヒロイン": "專案女主", "魔王": "魔王"}


def test_translate_corrupt_global_warns_but_works(home, tmp_path, caplog):
    home.mkdir(parents=True)
    (home / "glossary.json").write_text("{bad", encoding="utf-8")
    project = _project(tmp_path, [TextUnit(id="u1", source="こんにちは")])
    fake = FakeTranslator()

    with caplog.at_level(logging.WARNING):
        result = translate_project(project, fake)
    assert result.translated == 1
    assert fake.glossary == {}
    assert "corrupt global glossary" in caplog.text.lower()


TRANSFORMS_TOML = """\
[[transform]]
type = "opencc"
config = "s2twp"
custom_dict = "proj-terms.tsv"
"""


def test_transform_uses_global_terms_project_wins(home, tmp_path):
    pytest.importorskip("opencc")
    home.mkdir(parents=True)
    (home / "terms.tsv").write_text("軟體\t全域的軟\n資訊\t全域的訊\n", encoding="utf-8")

    project = _project(tmp_path, [TextUnit(id="u1", source="s", target="软件和信息",
                                           status=Status.MT)])
    (project.root / "proj-terms.tsv").write_text("軟體\t專案的軟\n", encoding="utf-8")
    toml = project.root / "project.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + "\n" + TRANSFORMS_TOML,
                    encoding="utf-8")

    assert runner.invoke(cli.app, ["transform", str(project.root)]).exit_code == 0
    [sf] = project.load_string_files()
    assert sf.by_id("u1").target == "專案的軟和全域的訊"
    again = runner.invoke(cli.app, ["transform", str(project.root)])
    assert "Transformed 0" in again.stdout


def test_glossary_add_global_creates_dir_and_file(home, tmp_path):
    project = _project(tmp_path, [])
    result = runner.invoke(cli.app, ["glossary", "add", "--global", "セーブ", "存檔",
                                     "--project", str(project.root)])
    assert result.exit_code == 0, result.output
    saved = Glossary.load(home / "glossary.json")
    assert saved.as_mapping() == {"セーブ": "存檔"}


def test_glossary_list_marks_origins(home, tmp_path):
    home.mkdir(parents=True)
    Glossary(entries=[GlossaryEntry(source="A", target="全A"),
                      GlossaryEntry(source="B", target="全B")]).save(home / "glossary.json")
    project = _project(tmp_path, [])
    runner.invoke(cli.app, ["glossary", "add", "A", "專A", "--project", str(project.root)])

    out = runner.invoke(cli.app, ["glossary", "list", "--project", str(project.root)]).stdout
    assert "A => 專A  (project)" in out
    assert "A => 全A  (shadowed)" in out
    assert "B => 全B  (global)" in out
