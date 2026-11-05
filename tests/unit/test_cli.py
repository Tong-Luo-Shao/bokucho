"""Tests for the CLI — primarily the Phase 0 acceptance: `bokucho init`."""

from typer.testing import CliRunner

from bokucho import __version__
from bokucho.cli import app
from bokucho.project import Project
from bokucho.translate.glossary import Glossary

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_init_scaffolds_project(tmp_path):
    target = tmp_path / "mygame"
    result = runner.invoke(
        app,
        [
            "init",
            str(target),
            "--name",
            "mygame-zh-hant",
            "--source-lang",
            "ja",
            "--target-lang",
            "zh-Hant",
        ],
    )
    assert result.exit_code == 0, result.stdout

    assert (target / "project.toml").is_file()
    assert (target / "strings").is_dir()
    assert (target / "glossary.json").is_file()

    project = Project.load(target)
    assert project.config.name == "mygame-zh-hant"
    assert project.config.source_lang == "ja"
    assert project.config.target_lang == "zh-tw"
    assert project.config.engine.type == "livemaker"
    assert project.config.translator.provider == "claude"

    assert Glossary.load(project.glossary_path).entries == []


def test_init_refuses_overwrite_without_force(tmp_path):
    first = runner.invoke(app, ["init", str(tmp_path)])
    assert first.exit_code == 0
    second = runner.invoke(app, ["init", str(tmp_path)])
    assert second.exit_code == 1
    third = runner.invoke(app, ["init", str(tmp_path), "--force"])
    assert third.exit_code == 0
