"""LL_LanguageOption text is read without treating plugin settings as prose."""

import json

import pytest

from bokucho.application.contracts import ProgressLevel
from bokucho.application.errors import InvalidProject
from bokucho.application.mount_patch import write_rpgmaker_mount
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.engines.rpgmaker import RpgMakerAdapter, RpgMakerWriteError
from bokucho.model import StringFile
from bokucho.project import Project


def _languages():
    return json.dumps(
        [
            json.dumps({"name": "日本語", "termCommands": ""}),
            json.dumps({"name": "English", "termCommands": json.dumps({"18": "New Game"})}),
            json.dumps(
                {
                    "name": "中文(簡体字）",
                    "termCommands": json.dumps({"18": "新游戏", "19": "继续"}),
                    "termMessages": json.dumps({"changeLanguage": "语言开关"}),
                    "types": json.dumps({"skillTypes": json.dumps(["魔法", "必杀技"])}),
                    "titleText": "游戏标题",
                    "currencyUnit": "金币",
                    "titles1": "ChineseTitleImage",
                }
            ),
        ]
    )


@pytest.fixture(params=["mv", "mz"])
def game(tmp_path, request):
    root = tmp_path / "game"
    base = root / "www" if request.param == "mv" else root
    (base / "data").mkdir(parents=True)
    (base / "js").mkdir()
    if request.param == "mz":
        (base / "js" / "rmmz_core.js").write_text("// core", encoding="utf-8")
    (base / "data" / "System.json").write_text('{"gameTitle":"Original"}', encoding="utf-8")
    (base / "data" / "Actors.json").write_text(
        json.dumps(
            [
                None,
                {
                    "id": 1,
                    "name": "スズ",
                    "note": "<name_1:Suzu>\n<name_2:旧名><name_2:小铃>\n"
                    "<profile_2:角色简介><script:leave_me_alone><BeforeCommon:21>",
                },
            ]
        ),
        encoding="utf-8",
    )
    registry = [
        {"name": "OtherPlugin", "status": True, "parameters": {"text": "Unrelated"}},
        {
            "name": "LL_LanguageOption",
            "status": True,
            "parameters": {"languages": _languages(), "variableId": "13"},
        },
    ]
    (base / "js" / "plugins.js").write_bytes(
        (
            "// Registry [kept]\r\nvar $plugins = "
            + json.dumps(registry)
            + ";\r\n/* Bokucho translation: begin */\n$plugins.push({name: 'Bokucho'});\r\n"
        ).encode("utf-8")
    )
    return root, base


def _registry(path):
    text = path.read_text(encoding="utf-8")
    return json.JSONDecoder().raw_decode(text.split("var $plugins = ", 1)[1])[0]


def test_reads_localized_notes_and_nested_plugin_text_with_stable_ids(game):
    root, _ = game
    units = RpgMakerAdapter().extract(root)
    by_id = {unit.id: unit for unit in units}
    assert by_id["Actors:1:name"].source == "スズ"
    assert by_id["Actors:1:note:name_2"].source == "小铃"
    assert by_id["Actors:1:note:profile_2"].source == "角色简介"
    assert by_id["Plugins:LL_LanguageOption:languages:2:termCommands:18"].source == "新游戏"
    assert by_id["Plugins:LL_LanguageOption:languages:2:termMessages:changeLanguage"].source == (
        "语言开关"
    )
    assert by_id["Plugins:LL_LanguageOption:languages:2:types:skillTypes:1"].source == "必杀技"
    sources = {unit.source for unit in units}
    assert {"Suzu", "New Game", "游戏标题", "金币"} <= sources
    assert not {"旧名", "ChineseTitleImage", "Unrelated", "leave_me_alone", "13"} & sources
    assert len(by_id) == len(units)
    assert [unit.id for unit in units] == [unit.id for unit in RpgMakerAdapter().extract(root)]


def test_injection_preserves_other_tags_languages_and_registry_code(game, tmp_path):
    root, base = game
    adapter = RpgMakerAdapter()
    units = adapter.extract(root)
    targets = {
        "Actors:1:note:name_2": "小鈴",
        "Actors:1:note:profile_2": "角色簡介",
        "Plugins:LL_LanguageOption:languages:2:termCommands:18": "新遊戲",
        "Plugins:LL_LanguageOption:languages:2:termMessages:changeLanguage": "語言切換",
        "Plugins:LL_LanguageOption:languages:2:types:skillTypes:1": "必殺技",
    }
    for unit in units:
        unit.target = targets.get(unit.id, "")
    original = {path: path.read_bytes() for path in base.rglob("*") if path.is_file()}
    out = tmp_path / "out"
    adapter.inject(units, root, out)
    output_base = out / base.relative_to(root)
    actors = json.loads((output_base / "data" / "Actors.json").read_text(encoding="utf-8"))
    assert actors[1]["name"] == "スズ"
    assert (
        actors[1]["note"] == "<name_1:Suzu>\n<name_2:旧名><name_2:小鈴>\n"
        "<profile_2:角色簡介><script:leave_me_alone><BeforeCommon:21>"
    )
    registry_path = output_base / "js" / "plugins.js"
    registry = _registry(registry_path)
    languages = [json.loads(value) for value in json.loads(registry[1]["parameters"]["languages"])]
    assert json.loads(languages[2]["termCommands"])["18"] == "新遊戲"
    assert json.loads(languages[2]["termCommands"])["19"] == "继续"
    assert json.loads(languages[2]["termMessages"])["changeLanguage"] == "語言切換"
    assert json.loads(json.loads(languages[2]["types"])["skillTypes"])[1] == "必殺技"
    assert json.loads(languages[1]["termCommands"])["18"] == "New Game"
    assert registry[1]["parameters"]["variableId"] == "13"
    assert registry[0] == _registry(base / "js" / "plugins.js")[0]
    registry_text = registry_path.read_bytes().decode("utf-8")
    original_text = (base / "js" / "plugins.js").read_bytes().decode("utf-8")
    marker = "var $plugins = "
    _, end = json.JSONDecoder().raw_decode(original_text, original_text.index(marker) + len(marker))
    assert registry_text.endswith(original_text[end:])
    assert registry_text.startswith("// Registry [kept]\r\nvar $plugins = ")
    assert original == {path: path.read_bytes() for path in original}


def test_malformed_registry_warns_without_losing_database_text(game):
    root, base = game
    (base / "js" / "plugins.js").write_text("var $plugins = [broken", encoding="utf-8")
    events = []

    class Sink:
        def publish(self, event):
            events.append(event)

    units = RpgMakerAdapter().extract(root, Sink())
    assert any(unit.source == "スズ" for unit in units)
    assert any(
        event.level is ProgressLevel.WARNING and "plugins.js" in event.detail for event in events
    )


def test_note_translation_cannot_break_the_metadata_tag(game, tmp_path):
    root, _ = game
    adapter = RpgMakerAdapter()
    units = adapter.extract(root)
    next(unit for unit in units if unit.id == "Actors:1:note:name_2").target = "bad>name"
    out = tmp_path / "out"
    with pytest.raises(RpgMakerWriteError, match="cannot contain"):
        adapter.inject(units, root, out)
    assert not out.exists()


def test_note_mounts_work_and_parameter_mounts_report_the_loading_limit(game, tmp_path):
    root, _ = game
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "language", "ja", "zh-Hant", "rpgmaker")
    )
    project.save_config(
        project.config.model_copy(
            update={
                "engine": project.config.engine.model_copy(update={"game_path": str(root)}),
            }
        )
    )
    project = Project.load(project.root)
    units = RpgMakerAdapter().extract(root)
    notes = [
        unit
        for unit in units
        if unit.id
        in (
            "Actors:1:note:name_2",
            "Actors:1:note:profile_2",
        )
    ]
    for unit, target in zip(notes, ("小鈴", "角色簡介"), strict=True):
        unit.target = target
    project.save_string_file(StringFile(source_file="Actors", units=notes), "actors.json")
    destination = tmp_path / "mount"
    assert write_rpgmaker_mount(project, destination)
    script = next(destination.rglob("Bokucho.js")).read_text(encoding="utf-8")
    line = next(line for line in script.splitlines() if "var translations = " in line)
    changes = json.loads(line.split(" = ", 1)[1].removesuffix(";"))["Actors.json"]
    assert len(changes) == 2
    assert changes[0][0]["path"] == [1, "note"]
    assert changes[1][0]["source"] == changes[0][0]["target"]
    plugin = next(unit for unit in units if unit.source == "语言开关")
    plugin.target = "語言切換"
    project.save_string_file(StringFile(source_file="Plugins", units=[plugin]), "plugins.json")
    with pytest.raises(InvalidProject, match="bokucho inject"):
        write_rpgmaker_mount(project, tmp_path / "unsupported")
