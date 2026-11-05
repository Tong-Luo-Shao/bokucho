"""Tests for the Phase 4 QA checks and CLI report."""

import json

import pytest
from typer.testing import CliRunner

from bokucho import cli
from bokucho.application.quality import qa_report
from bokucho.application.verify import verify_project
from bokucho.model import Context, Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.qa import (
    check_unit,
    dropped_lines,
    foreign_text,
    format_issues,
    format_problems,
    missing_control_codes,
    problem_marks,
    translation_tokens,
)

runner = CliRunner()


def _pairs(problems):
    return [(problem.check, problem.message) for problem in problems]


def test_check_unit_reports_each_defect():
    unit = TextUnit(
        id="u1",
        source="一行\n%name%二行",
        target="併成一行沒有變數,你好",
        status=Status.MT,
        context=Context(max_length=5),
    )
    checks = {problem.check for problem in check_unit(unit)}
    assert checks == {"variables", "length"}


def test_check_unit_clean():
    unit = TextUnit(id="u1", source="おはよう", target="早安", status=Status.MT)
    assert check_unit(unit) == []


def test_qa_does_not_flag_cp932_characters_that_release_maps_automatically(tmp_path):
    project = _project(
        tmp_path,
        [TextUnit(id="u1", source="こんにちは", target="您好貓", status=Status.MT)],
    )

    result = runner.invoke(cli.app, ["qa", str(project.root)])

    assert result.exit_code == 0, result.stdout
    assert "encoding" not in result.stdout.lower()


def _project(tmp_path, units) -> Project:
    proj_dir = tmp_path / "proj"
    assert runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"]).exit_code == 0
    project = Project.load(proj_dir)
    project.save_string_file(StringFile(source_file="a.lsb", units=units), "a.lsb.json")
    return project


def test_qa_clean_project_exits_zero(tmp_path):
    project = _project(
        tmp_path,
        [
            TextUnit(id="u1", source="おはよう", target="早安", status=Status.REVIEWED),
            TextUnit(id="u2", source="さようなら"),
        ],
    )
    result = runner.invoke(cli.app, ["qa", str(project.root)])
    assert result.exit_code == 0, result.stdout
    assert "QA passed" in result.stdout
    assert "untranslated: 1" in result.stdout


def test_qa_defects_exit_one_and_json_report(tmp_path):
    project = _project(
        tmp_path,
        [TextUnit(id="u1", source="%name%は言った", target="名前", status=Status.MT)],
    )
    report = project.root / "qa.json"
    result = runner.invoke(cli.app, ["qa", str(project.root), "--json", str(report)])
    assert result.exit_code == 1
    assert "[variables]" in result.stdout

    issues = json.loads(report.read_text(encoding="utf-8"))
    assert len(issues) == 1
    assert issues[0]["unit_id"] == "u1"
    assert issues[0]["check"] == "variables"
    assert issues[0]["severity"] == "error"


def test_line_count_difference_is_a_warning_and_does_not_fail_qa(tmp_path):
    project = _project(
        tmp_path,
        [TextUnit(id="u1", source="一行\n二行", target="合併成一行", status=Status.MT)],
    )
    result = runner.invoke(cli.app, ["qa", str(project.root)])

    assert result.exit_code == 0, result.stdout
    assert "[line-count]" in result.stdout
    assert "warning" in result.stdout

    report = Project.load(project.root)
    from bokucho.application.quality import run_qa

    (issue,) = run_qa(report).issues
    assert issue.severity == "warning"
    assert issue.message == "line count changed: 2 -> 1"


def test_qa_without_extract_errors(tmp_path):
    proj_dir = tmp_path / "empty"
    runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"])
    result = runner.invoke(cli.app, ["qa", str(proj_dir)])
    assert result.exit_code == 1


def test_format_flags_lost_renpy_tokens():
    assert format_issues("Hi [player]!", "嗨!", engine="renpy")
    assert format_issues("{i}really{/i} important", "真的很重要", engine="renpy")
    assert format_issues("Hi [player]!", "[player],嗨!", engine="renpy") == []
    assert format_issues("{i}really{/i}", "{i}真的{/i}", engine="renpy") == []


def test_format_tokens_are_noop_for_other_engines():
    assert format_issues("おはよう%name%", "早安%name%") == []


def test_numbered_placeholders_are_each_their_own_variable():
    assert format_issues("%1は%2の経験値を獲得！", "%1獲得了%2點經驗值！") == []
    assert format_issues("%1は%2の経験値を獲得！", "獲得了經驗值！") == [
        "missing variables: %1, %2"
    ]


def test_two_percentages_are_not_a_variable():
    assert format_issues("HPが50%と30%回復", "HP回復了50%和30%") == []


def test_brackets_off_a_picture_are_not_a_variable():
    source = "- old gray wolf -\n村のはずれ[ウルフ]"
    target = "- old gray wolf -\n村外[灰狼]"
    assert format_issues(source, target, engine="livemaker") == []
    assert format_issues(source, target, engine="renpy")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Wait\\!then go", "等一下然後走"),
        ("You have \\V[3] gold", "你有金幣"),
        (r"\{Big\}text", "text"),
        (r"Cost: \G gold", "花費"),
    ],
)
def test_a_dropped_rpg_maker_code_is_reported(source, target):
    assert missing_control_codes(source, target, "rpgmaker")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (r"\C[4]火\C[0]を放つ", "放出火焰"),
        (r"\c[2]炎\c[0]", "火焰"),
        (r"\v[1]ゴールド", "金幣"),
    ],
)
def test_a_dropped_rpg_maker_code_fails_the_format_gate(source, target):
    assert format_problems(source, target, engine="rpgmaker-mv")[0].check == "variables"


def test_lowercase_rpg_maker_codes_are_protected():
    assert translation_tokens(r"\v[1]は\c[2]炎\c[0]", "rpgmaker-mv") == [
        {"token": r"\v[1]", "kind": "variable", "count": 1},
        {"token": r"\c[2]", "kind": "control", "count": 1},
        {"token": r"\c[0]", "kind": "control", "count": 1},
    ]


def test_a_kept_colour_code_is_fine():
    assert missing_control_codes("\\C[14]Campfire\\C[0]", "\\C[14]營地火\\C[0]", "rpgmaker") == []


@pytest.mark.parametrize("engine", ["rpgmaker-mv", "rpgmaker-mz", "rpgmaker-vx-ace"])
def test_concrete_rpg_maker_types_keep_control_code_checks(engine):
    assert missing_control_codes(r"Wait\!then go", "等一下然後走", engine) == [r"\!"]


def test_other_engines_are_not_checked_for_rpg_maker_codes():
    assert missing_control_codes('He said \\"hi\\".', "他說「嗨」。", "renpy") == []


def test_a_windows_path_is_not_reported_as_a_dropped_code():
    source = r"Save at C:\Users\Player\save.dat"
    target = "存檔位置：C:/Users/Player/save.dat"
    assert missing_control_codes(source, target, "rpgmaker") == []


def test_translation_tokens_describe_engine_tokens_in_source_order():
    assert translation_tokens(
        r"Hi %name%, [player] {i} \V[2] \C[4] \V[2]",
        "rpgmaker",
    ) == [
        {"token": "%name%", "kind": "variable", "count": 1},
        {"token": r"\V[2]", "kind": "variable", "count": 2},
        {"token": r"\C[4]", "kind": "control", "count": 1},
    ]


def test_translation_tokens_split_numbered_placeholders():
    assert translation_tokens("%1は%2の経験値を獲得！", "rpgmaker-mv") == [
        {"token": "%1", "kind": "variable", "count": 1},
        {"token": "%2", "kind": "variable", "count": 1},
    ]


def test_translation_tokens_include_renpy_interpolation_and_formatting():
    assert translation_tokens("Hi [player] {i}text{/i}", "renpy") == [
        {"token": "[player]", "kind": "variable", "count": 1},
        {"token": "{i}", "kind": "formatting", "count": 1},
        {"token": "{/i}", "kind": "formatting", "count": 1},
    ]


def test_a_code_kept_once_but_needed_twice_is_reported():
    assert missing_control_codes(r"\C[14]A\C[14]B", r"\C[14]A B", "rpgmaker") == [r"\C[14]"]


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("「うーん、ねむいーー！！」", "「嗯，好睏喔ーー！！」"),
        ("「えいっ。絵本出しちゃえっ！！」", "「嘿。把ㄅㄆㄇ拿出來！！」"),
        ("ｈｉｎｔ！入手した装備品", "ｈｉｎｔ！入手的裝備品"),
        ("のEXPを獲得！", "獲得了的exp！"),
        ("佐々木", "佐々木"),
        ("このお餅、モチモチだ", "這塊麻糬真Q彈啊"),
        ("長いゲームだな", "這款遊戲的PLAY時間真長啊"),
        ("このシャツは大きいな", "這件襯衫是L號的呢"),
        ("トムのハツカネズミ", "Tom的二十日鼠"),
        ("ピピピのQ", "PiPiPi的Q"),
        ("ザ・ランナーズ", "The Runners"),
    ],
)
def test_what_a_chinese_target_may_carry(source, target):
    assert foreign_text(source, target, "zh-Hant-TW") == []


@pytest.mark.parametrize(
    ("source", "target", "foreign"),
    [
        ("庭で本を読む", "在庭院initialized裡看書", ["initialized"]),
        ("門が開いたっ……！", "NIL", ["NIL"]),
        ("子供はボールを投げた！", "J1_000020B4_0", ["J", "B"]),
        ("「はぁー、暖かい」", "「ha——，好溫暖」", ["ha"]),
        ("古い鍵が壊れた。", "舊鑰匙ки壞了。", ["ки"]),
        ("鍵が壊れた。", "Ключ壞了。", ["Ключ"]),
        ("「寒いっ……」", "<span>冷……</span>", ["span"]),
        ("「えいっ」", "「嘿っ」", ["っ"]),
        ("「……！？\nあなた」", "「……！？\\n你」", ["\\n"]),
    ],
)
def test_what_a_chinese_target_may_not_carry(source, target, foreign):
    assert foreign_text(source, target, "zh-Hant-TW") == foreign


def test_a_language_with_no_known_script_is_not_judged():
    assert foreign_text("はい", "Да", "ru") == []


def test_chinese_characters_left_in_a_latin_target_are_foreign():
    assert foreign_text("回復薬を使った", "I used the 回復薬", "en") == ["回復薬"]
    assert problem_marks("回復薬を使った", "I used the 回復薬", target_lang="en") == [
        "回",
        "復",
        "薬",
    ]
    assert foreign_text("回復薬を使った", "Использовал 回復薬", "ru") == ["回復薬"]


@pytest.mark.parametrize(
    ("source", "target", "dropped"),
    [
        ("を使った！\n武力が増加した！", "！\n武力提升了！", ["を使った！"]),
        ("らしい。", "。", ["らしい。"]),
        ("　を使った！", "！", ["を使った！"]),
        ("……っ！\n行くぞ！", "……！\n上吧！", []),
        ("ーーっ！", "——！", []),
        ("大魔導士アオイ\nである。", "便是大魔導士阿奧伊\n。", []),
        ("雨が\n降ったとも言われる。", "也曾下過雨\n。", []),
        ("行くぞ。\nらしい。", "上吧。\n。", ["らしい。"]),
        ("を使った！\n武力が増加した！", "武力提升了！", []),
        ("を使った！", "使用了！", []),
    ],
)
def test_a_line_that_kept_only_its_punctuation_dropped_its_words(source, target, dropped):
    assert dropped_lines(source, target) == dropped


def test_dropped_words_are_a_problem_of_their_own():
    assert _pairs(format_problems("を使った！", "！", target_lang="zh-Hant-TW")) == [
        ("dropped", "words dropped from: を使った！")
    ]


def test_format_warnings_ignore_a_terminal_newline():
    from bokucho.qa import format_warnings

    assert format_warnings("一行\n二行\n", "一行\n二行") == []
    assert [finding.message for finding in format_warnings("一行\n二行", "合併成一行")] == [
        "line count changed: 2 -> 1"
    ]


def test_livemaker_variables_are_protected_tokens():
    assert translation_tokens("<VAR 【名】>は<VAR 【名】>", "livemaker") == [
        {"token": "<VAR 【名】>", "kind": "variable", "count": 2}
    ]


def test_a_dropped_livemaker_variable_is_a_variables_problem():
    assert format_problems("<VAR 【名】>を", "把", engine="livemaker")[0].check == "variables"


def test_a_variable_kept_once_of_twice_is_missing():
    assert _pairs(format_problems("%a% と %a%", "%a%", engine=""))[0] == (
        "variables",
        "missing variables: %a%",
    )


def test_an_invented_livemaker_variable_is_a_variables_problem():
    problems = format_problems("<VAR 【名】>を", "<VAR 【名】><VAR 【他】>把", engine="livemaker")
    assert _pairs(problems) == [("variables", "unexpected variables: <VAR 【他】>")]


def test_var_tags_are_not_variables_outside_livemaker():
    assert all(
        problem.check != "variables"
        for problem in format_problems("<VAR x>", "y", engine="rpgmaker")
    )


def test_a_kana_variable_name_is_not_foreign_text():
    assert format_problems(
        "<VAR 【入手品1の内容】>を<VAR 【入手品1】>個、手に入れた。",
        "得到了<VAR 【入手品1】>個<VAR 【入手品1の内容】>。",
        engine="livemaker",
        target_lang="zh-Hant",
    ) == []


def test_a_percent_sign_in_livemaker_text_is_not_an_unexpected_variable():
    assert format_problems("成功率は50%です。", "成功率50%，失敗率50%。", engine="livemaker") == []


def test_problem_marks_point_at_the_kana_left_in_a_chinese_line():
    source = "【アオイ】\n今日も…雨が降っているようですね。"
    target = "【阿奧伊】\n今天…好像也在下雨呢ね。"

    assert problem_marks(source, target, True, "livemaker", "zh-Hant") == ["ね"]


def test_problem_marks_point_at_a_variable_the_source_does_not_have():
    source = "<VAR 【品名】>を手に入れた。"
    target = "得到了<VAR 【品名】><VAR 【個数】>。"

    assert problem_marks(source, target, True, "livemaker", "zh-Hant") == ["<VAR 【個数】>"]


def test_problem_marks_take_the_whole_line_handed_back():
    assert problem_marks("げんき", "げんき", True, "", "zh-Hant") == ["げんき"]


def test_a_line_that_passes_has_nothing_to_mark():
    assert problem_marks("げんき", "很好", True, "", "zh-Hant") == []



def test_brackets_a_translation_changed_are_a_warning_naming_the_fix(tmp_path):
    from bokucho.application.quality import run_qa

    project = _project(
        tmp_path, [TextUnit(id="u1", source="「やめて」", target="（住手）", status=Status.MT)]
    )
    (issue,) = run_qa(project).issues
    assert (issue.check, issue.severity) == ("brackets", "warning")
    assert issue.message.endswith("fix: 「住手」")


def test_a_bracket_warning_sits_on_the_line_that_lost_the_bracket(tmp_path):
    from bokucho.application.quality import run_qa

    project = _project(
        tmp_path,
        [
            TextUnit(
                id="pylm:text:a.lsb:5:0", source="「はい」", target="「是」", status=Status.MT
            ),
            TextUnit(id="pylm:text:a.lsb:5:1", source="「あ」", target="「啊", status=Status.MT),
        ],
    )
    project.config.engine.type = "livemaker"
    (issue,) = run_qa(project).issues
    assert issue.unit_id == "pylm:text:a.lsb:5:1"


def test_brackets_are_not_held_against_a_language_that_quotes_otherwise(tmp_path):
    from bokucho.application.quality import run_qa

    project = _project(
        tmp_path, [TextUnit(id="u1", source="「やめて」", target='"Stop"', status=Status.MT)]
    )
    project.config.target_lang = "en"
    assert run_qa(project).issues == ()


def test_bracket_fixes_touch_machine_drafts_and_nothing_a_person_has_answered(tmp_path):
    from bokucho.application.quality import bracket_fix_report, fix_brackets

    project = _project(
        tmp_path,
        [
            TextUnit(id="u1", source="「やめて」", target="（住手）", status=Status.MT),
            TextUnit(id="u2", source="「あっ」", target="（啊）", status=Status.REVIEWED),
        ],
    )
    assert bracket_fix_report(project, project.load_string_files()).fixed == 1

    assert fix_brackets(project).fixed == 1

    saved = Project.load(project.root).load_string_files()[0].units
    assert [(unit.target, unit.status) for unit in saved] == [
        ("「住手」", Status.MT),
        ("（啊）", Status.REVIEWED),
    ]
    assert bracket_fix_report(project, project.load_string_files()).fixed == 0


def _script(unit_id, source, target="", status=Status.MT, **ref):
    return TextUnit(id=unit_id, source=source, target=target, status=status, kind="script",
                    engine_ref={"file": "Scripts", "address": unit_id, "kind": "script", **ref})


def test_a_script_literal_that_loses_a_specifier_is_an_error():
    problems = check_unit(_script("s0:1:0", "%sが出現！", "出現了！"), engine="rpgmaker-vx")
    assert [(p.check, p.code, dict(p.params)) for p in problems] == [
        ("variables", "variables_missing", {"missing": "%s"}),
    ]


def test_a_percent_in_a_script_literal_without_specifiers_is_fine():
    assert check_unit(_script("s0:1:0", "Lv", "等級%"), engine="rpgmaker-xp") == []


def test_a_dialogue_line_keeps_todays_rules():
    unit = TextUnit(id="u", source="%sが来た", target="來了", status=Status.MT)
    assert [p.check for p in check_unit(unit, engine="rpgmaker-xp")] == []


def test_a_compared_word_shipped_two_ways_is_warned(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:a", "Miss", "未命中", key=True),
        _script("Scripts:b", "Miss", "", status=Status.IGNORED, key=True),
    ])
    report = qa_report(project, project.load_string_files())
    warned = {(i.unit_id, i.severity) for i in report.issues if i.code == "script_key"}
    assert warned == {("Scripts:a", "warning"), ("Scripts:b", "warning")}


def test_a_database_name_a_script_compares_is_warned_when_translated(tmp_path):
    project = _project(tmp_path, [
        TextUnit(id="Items:1:name", source="ポーション", target="藥水", status=Status.MT),
        _script("Scripts:k", "ポーション", "", status=Status.IGNORED, key=True),
    ])
    issues = qa_report(project, project.load_string_files()).issues
    [issue] = [i for i in issues if i.code == "script_key"]
    assert (issue.unit_id, issue.params["where"], issue.params["elsewhere"]) == (
        "Scripts:k", "Items:1:name", "藥水",
    )


def test_a_compared_word_shipped_one_way_is_quiet(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:a", "Miss", "", status=Status.IGNORED, key=True),
        _script("Scripts:b", "Miss", "", status=Status.IGNORED, key=True),
    ])
    issues = qa_report(project, project.load_string_files()).issues
    assert not [i for i in issues if i.code == "script_key"]


def test_a_translated_literal_naming_a_game_file_is_warned(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:z", "A1 決定", "A1 決定音", asset="Audio/SE/A1 決定.ogg"),
    ])
    issues = qa_report(project, project.load_string_files()).issues
    [issue] = [i for i in issues if i.code == "script_asset"]
    assert (issue.severity, issue.params["file"]) == ("warning", "Audio/SE/A1 決定.ogg")


def test_an_ignored_compared_word_with_a_target_ships_it_and_is_warned(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:a", "Miss", "未命中", status=Status.IGNORED, key=True),
        _script("Scripts:b", "Miss", "", status=Status.IGNORED, key=True),
    ])
    report = qa_report(project, project.load_string_files())
    warned = {(i.unit_id, i.severity) for i in report.issues if i.code == "script_key"}
    assert warned == {("Scripts:a", "warning"), ("Scripts:b", "warning")}


def test_an_ignored_file_name_with_a_target_ships_it_and_is_warned(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:z", "x", "ｘ", status=Status.IGNORED, asset="Audio/SE/x.ogg"),
    ])
    issues = qa_report(project, project.load_string_files()).issues
    [issue] = [i for i in issues if i.code == "script_asset"]
    assert (issue.unit_id, issue.severity) == ("Scripts:z", "warning")


def test_a_file_name_with_no_target_is_quiet(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:z", "A1 決定", "", asset="Audio/SE/A1 決定.ogg"),
    ])
    issues = qa_report(project, project.load_string_files()).issues
    assert not [i for i in issues if i.code == "script_asset"]


def test_a_script_percent_sign_after_a_specifier_is_not_read_as_a_variable():
    unit = _script("s0:1:0", "%d%%ダメージ", "傷害 %d %%")
    assert check_unit(unit, engine="rpgmaker-vx") == []


def test_the_script_warnings_read_in_english(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:a", "Miss", "未命中", key=True),
        _script("Scripts:b", "Miss", "", status=Status.IGNORED, key=True),
        _script("Scripts:z", "A1 決定", "A1 決定音", asset="Audio/SE/A1 決定.ogg"),
    ])
    messages = [i.message for i in qa_report(project, project.load_string_files()).issues]
    assert messages == [
        "a script compares Miss: shipped here as 未命中, as Miss at Scripts:b",
        "a script compares Miss: shipped here as Miss, as 未命中 at Scripts:a",
        "A1 決定 names a game file: Audio/SE/A1 決定.ogg",
    ]


def test_a_unit_the_game_no_longer_has_is_not_what_a_script_compares_with(tmp_path):
    gone = _script("Scripts:gone", "Miss", "命中", key=True)
    gone.missing = True
    project = _project(tmp_path, [
        _script("Scripts:a", "Miss", "", status=Status.IGNORED, key=True),
        gone,
    ])
    issues = qa_report(project, project.load_string_files()).issues
    assert not [i for i in issues if i.code == "script_key"]


def test_verify_flags_a_script_unit_that_dropped_an_interpolation(tmp_path):
    project = _project(tmp_path, [
        _script("Scripts:v", "#{name}が出現", "出現了"),
        TextUnit(id="u1", source="#{name}が出現", target="出現了", status=Status.MT),
    ])
    [tags] = [p for p in verify_project(project).properties if p.key == "tags"]
    assert tags.failures == (("a.lsb Scripts:v", "missing variables: #{name}"),)


def test_script_marks_point_at_a_placeholder_the_source_does_not_have():
    marks = problem_marks("%sが出現！", "%s和%d出現了！", engine="rpgmaker-vx", script=True)
    assert marks == ["%d"]
    assert problem_marks("%sが出現！", "%s和%d出現了！", engine="rpgmaker-vx") == []
