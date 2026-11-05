"""Tests for Phase 5: transforms, import, review, hans-residue QA."""

import json

import pytest
from typer.testing import CliRunner

from bokucho import cli
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.transform import available_transforms, get_transform

runner = CliRunner()

pytest.importorskip("opencc", reason="opencc not installed")




def test_registry():
    assert {"opencc", "punctuation"} <= set(available_transforms())
    with pytest.raises(ValueError, match="Unknown transform"):
        get_transform("nope")


def test_opencc_localizes_and_is_idempotent():
    t = get_transform("opencc", config="s2twp")
    assert t.convert("软件和服务器的信息") == "軟體和伺服器的資訊"
    once = t.convert("鼠标点击视频窗口")
    assert t.convert(once) == once


def test_opencc_custom_dict_overrides(tmp_path):
    dict_file = tmp_path / "terms.tsv"
    dict_file.write_text("# game terms\n軟體\t軟體XY\n", encoding="utf-8")
    t = get_transform("opencc", config="s2twp", custom_dict=dict_file)
    assert t.convert("软件") == "軟體XY"


def test_punctuation_quotes():
    t = get_transform("punctuation", profile="zh-Hant-TW")
    assert t.convert("他说:“真‘好’啊”") == "他说:「真『好』啊」"
    assert t.convert(t.convert("“x”")) == "「x」"
    with pytest.raises(ValueError, match="Unknown punctuation profile"):
        get_transform("punctuation", profile="nope")




def _project(tmp_path, units, transforms: str = "") -> Project:
    proj_dir = tmp_path / "proj"
    assert runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"]).exit_code == 0
    if transforms:
        toml = proj_dir / "project.toml"
        toml.write_text(toml.read_text(encoding="utf-8") + "\n" + transforms, encoding="utf-8")
    project = Project.load(proj_dir)
    project.save_string_file(StringFile(source_file="a.lsb", units=units), "a.lsb.json")
    return project


TRANSFORMS_TOML = """\
[[transform]]
type = "opencc"
config = "s2twp"

[[transform]]
type = "punctuation"
profile = "zh-Hant-TW"
"""


def test_cli_transform_pipeline_and_idempotency(tmp_path):
    project = _project(
        tmp_path,
        [
            TextUnit(id="u1", source="s1", target="“软件”", status=Status.MT),
            TextUnit(id="u2", source="s2", target="信息", status=Status.REVIEWED),
        ],
        TRANSFORMS_TOML,
    )
    result = runner.invoke(cli.app, ["transform", str(project.root)])
    assert result.exit_code == 0, result.stdout

    [sf] = project.load_string_files()
    assert sf.by_id("u1").target == "「軟體」"
    assert sf.by_id("u1").status is Status.MT
    assert sf.by_id("u2").target == "信息"

    again = runner.invoke(cli.app, ["transform", str(project.root)])
    assert "Transformed 0" in again.stdout


def test_cli_transform_without_config_errors(tmp_path):
    project = _project(tmp_path, [TextUnit(id="u1", source="s", target="软")])
    assert runner.invoke(cli.app, ["transform", str(project.root)]).exit_code == 1


def test_cli_import_fills_mt_and_respects_protection(tmp_path):
    project = _project(
        tmp_path,
        [
            TextUnit(id="u1", source="こんにちは"),
            TextUnit(id="u2", source="ようこそ", target="人工", status=Status.REVIEWED),
        ],
    )
    mapping = tmp_path / "hans.json"
    mapping.write_text(
        json.dumps({"こんにちは": "你好", "ようこそ": "欢迎", "無關": "x"}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = runner.invoke(
        cli.app, ["import", str(mapping), "--project", str(project.root)]
    )
    assert result.exit_code == 0, result.stdout
    assert "Imported 1" in result.stdout
    assert "1 entry(ies) had no matching source" in result.stdout

    [sf] = project.load_string_files()
    assert sf.by_id("u1").target == "你好"
    assert sf.by_id("u1").status is Status.MT
    assert sf.by_id("u2").target == "人工"


def test_cli_review_lists_and_marks(tmp_path):
    project = _project(
        tmp_path,
        [
            TextUnit(id="u1", source="a", target="譯a", status=Status.MT),
            TextUnit(id="u2", source="b", target="譯b", status=Status.MT),
        ],
    )
    listing = runner.invoke(cli.app, ["review", str(project.root)])
    assert "u1" in listing.stdout and "2 unit(s)" in listing.stdout

    marked = runner.invoke(cli.app, ["review", str(project.root), "--mark", "u1"])
    assert marked.exit_code == 0, marked.stdout
    [sf] = project.load_string_files()
    assert sf.by_id("u1").status is Status.REVIEWED
    assert sf.by_id("u2").status is Status.MT


def test_qa_flags_hans_residue(tmp_path):
    project = _project(
        tmp_path,
        [TextUnit(id="u1", source="s", target="這裡有残留的简体", status=Status.MT)],
    )
    result = runner.invoke(cli.app, ["qa", str(project.root)])
    assert result.exit_code == 1
    assert "hans-residue" in result.stdout


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("为了你们着想", "為了你們著想"),
        ("犠牲を払った対価", "犧牲を拂った對價"),
        ("這傢伙只有一句話：不准動！", "這傢伙只有一句話：不准動！"),
        ("皇后被帶進了后院", "皇后被帶進了後院"),
        ("吚呀……嘞", "吚呀……嘞"),
        ("雪姫的呪縛", "雪姬的咒縛"),
        ("駄目……", "駄目……"),
        ("这里和嘴里，村子里", "這裡和嘴裡，村子裡"),
        ("你在哪里？在里面！", "你在哪裡？在裡面！"),
        ("萬里之外的阿里與巴里", "萬里之外的阿里與巴里"),
        ("鄰里的里長走了三公里，稀里嘩啦", "鄰里的里長走了三公里，稀里嘩啦"),
    ],
)
def test_the_residue_converter_moves_only_what_is_not_traditional(target, expected):
    from bokucho.application.quality import hans_converter

    assert hans_converter("zh-Hant-TW")(target) == expected


def test_qa_uses_traditional_direction_even_if_project_transform_does_not(tmp_path):
    project = _project(
        tmp_path,
        [TextUnit(id="u1", source="s", target="這是一段繁體中文", status=Status.MT)],
        '[[transform]]\ntype = "opencc"\nconfig = "t2s"\n',
    )

    result = runner.invoke(cli.app, ["qa", str(project.root)])

    assert result.exit_code == 0, result.stdout
    assert "hans-residue" not in result.stdout


def test_a_conversion_leaves_the_variables_it_is_told_to_keep():
    import re

    unit = TextUnit(
        id="u1",
        source="<VAR 【入手品1の内容】>を",
        target="把<VAR 【入手品1の内容】>信息",
        status=Status.MT,
    )

    get_transform("opencc", config="s2twp").apply(
        [unit], protected=(re.compile(r"<VAR [^<>\n]+>"),)
    )

    assert unit.target == "把<VAR 【入手品1の内容】>資訊"


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("<VAR 【呼び名：トモ】> 阿奧伊っ。", "<VAR 【呼び名：トモ】> 阿奧伊。"),
        ("选了<VAR 【対象】>。", "選了<VAR 【対象】>。"),
    ],
)
def test_a_settled_answer_leaves_the_variables_as_written(answer, expected):
    from bokucho.application.text import settle_answer

    assert settle_answer(answer, "zh-Hant-TW", "livemaker") == expected


def test_a_variable_named_in_a_japanese_form_is_not_residue():
    from bokucho.application.quality import hans_converter
    from bokucho.qa import check_unit

    unit = TextUnit(
        id="u1", source="<VAR 【対象】>を選んだ。", target="選了<VAR 【対象】>。", status=Status.MT
    )

    assert check_unit(unit, hans_converter("zh-Hant-TW"), engine="livemaker") == []
