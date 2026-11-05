"""Language identity is stored as language and region, without a script subtag."""

import tomllib

import pytest

from bokucho.application.quality import hans_converter
from bokucho.model import TextUnit
from bokucho.project import Project, ProjectConfig
from bokucho.translators.base import TranslationContext, build_prompt


@pytest.mark.parametrize("engine", ["rpgmaker", "renpy"])
def test_legacy_project_tags_are_normalized_before_prompting_and_saving(tmp_path, engine):
    path = tmp_path / "project.toml"
    path.write_text(
        '[project]\nname = "Legacy"\nsource_lang = "zh-Hans-CN"\n'
        f'target_lang = "zh-Hant-TW"\n[engine]\ntype = "{engine}"\n',
        encoding="utf-8",
    )
    project = Project.load(tmp_path)
    assert project.config.source_lang == "zh-cn"
    assert project.config.target_lang == "zh-tw"
    if engine == "renpy":
        assert project.config.engine.language == "zh_hant_tw"
    context = TranslationContext(
        source_lang=project.config.source_lang, target_lang=project.config.target_lang,
    )
    prompt, _ = build_prompt([TextUnit(id="1", source="新的冒险")], context, None)
    assert "Source: zh-cn. Target: zh-tw." in prompt
    assert "Hant" not in prompt and "Hans" not in prompt
    project.save_config(project.config)
    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert saved["project"]["source_lang"] == "zh-cn"
    assert saved["project"]["target_lang"] == "zh-tw"
    if engine == "renpy":
        assert saved["engine"]["language"] == "zh_hant_tw"


@pytest.mark.parametrize("source,target,expected", [
    ("sr-Latn-RS", "zh-Hant", ("sr-rs", "zh-tw")),
    ("ja_JP", "zh-Hans", ("ja-jp", "zh-cn")),
    ("en-x-test", "zh-Hant-HK", ("en-x-test", "zh-hk")),
])
def test_configuration_ignores_scripts_but_keeps_regions_and_private_tags(source, target, expected):
    config = ProjectConfig(name="Codes", source_lang=source, target_lang=target)
    assert (config.source_lang, config.target_lang) == expected


def test_region_targets_keep_traditional_conversion_and_mainland_text_separate():
    convert = hans_converter("zh-tw")
    assert convert is not None
    assert convert("这里有简体中文。") == "這裡有簡體中文。"
    assert hans_converter("zh-cn") is None
