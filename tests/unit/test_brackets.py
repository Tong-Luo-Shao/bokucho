"""A translation keeps the brackets of what it translates: who speaks, who thinks."""

from __future__ import annotations

from bokucho.brackets import bracket_findings
from bokucho.model import Status, TextUnit


def _unit(block: str, source: str, target: str, line: int = 5) -> TextUnit:
    return TextUnit(
        id=f"pylm:text:a.lsb:{line}:{block}", source=source, target=target, status=Status.MT
    )


def test_speech_turned_into_thought_is_put_back() -> None:
    (finding,) = bracket_findings([_unit("0", "「やめて」", "（住手）")], "livemaker")
    assert finding.kind == "speech-to-thought"
    assert (finding.source, finding.target) == ("「」", "（）")
    assert finding.fixes == {"pylm:text:a.lsb:5:0": "「住手」"}


def test_a_message_cut_into_units_is_read_as_one() -> None:
    units = [_unit("0", "「あ", "「啊"), _unit("1", "い」", "咿")]
    (finding,) = bracket_findings(units, "livemaker")
    assert finding.kind == "missing-or-extra"
    assert finding.unit_ids == ("pylm:text:a.lsb:5:0", "pylm:text:a.lsb:5:1")
    assert finding.fixes == {"pylm:text:a.lsb:5:1": "咿」"}


def test_brackets_added_are_dropped_where_the_source_has_none() -> None:
    (finding,) = bracket_findings([_unit("0", "「あ。　い」", "「啊。」　「咿」")], "livemaker")
    assert finding.fixes == {"pylm:text:a.lsb:5:0": "「啊。　咿」"}


def test_a_closer_dropped_inside_a_message_is_put_back_on_its_piece() -> None:
    units = [_unit("0", "「あ", "「啊"), _unit("1", "い」", "咿"), _unit("2", "「う」", "「嗚」")]
    (finding,) = bracket_findings(units, "livemaker")
    assert finding.fixes == {"pylm:text:a.lsb:5:1": "咿」"}


def test_a_speech_closed_at_each_of_its_lines_is_rejoined() -> None:
    units = [
        _unit("0", "「あ？\n　い」", "「啊？」\n　「咿」"),
        _unit("1", "「う", "「嗚"),
        _unit("2", "え」", "欸"),
    ]
    (finding,) = bracket_findings(units, "livemaker")
    assert finding.fixes == {
        "pylm:text:a.lsb:5:0": "「啊？\n　咿」",
        "pylm:text:a.lsb:5:2": "欸」",
    }


def test_a_line_inside_a_speech_loses_the_pair_put_around_it() -> None:
    unit = _unit("0", "「あ、\nい。\n　う」", "「啊，」\n「咿。」\n　「嗚」")
    (finding,) = bracket_findings([unit], "livemaker")
    assert finding.fixes == {"pylm:text:a.lsb:5:0": "「啊，\n咿。\n　嗚」"}


def test_quotes_put_around_a_line_of_narration_are_left_to_a_person() -> None:
    (finding,) = bracket_findings(
        [_unit("0", "　あ。\n　い。", "　「啊。」\n　「咿。」")], "livemaker"
    )
    assert finding.fixes == {}


def test_a_message_is_reported_on_the_pieces_whose_brackets_differ() -> None:
    units = [_unit("0", "「あ」", "「啊」"), _unit("1", "「い", "「咿"), _unit("2", "う」", "嗚")]
    (finding,) = bracket_findings(units, "livemaker")
    assert [(unit_id, found.params) for unit_id, found in finding.findings] == [
        ("pylm:text:a.lsb:5:2", {"source": "」", "target": None, "fixed": "嗚」"})
    ]


def test_a_pair_the_translation_added_is_left_to_a_person() -> None:
    unit = _unit("0", "「すごい技だ」", "「好（厲害）技術」")
    (finding,) = bracket_findings([unit], "livemaker")
    assert finding.kind == "missing-or-extra"
    assert finding.fixes == {}


def test_a_slip_in_the_source_is_not_copied_into_the_translation() -> None:
    (finding,) = bracket_findings([_unit("0", "「あ」」", "「啊」")], "livemaker")
    assert finding.fixes == {}


def test_a_line_that_lost_its_pair_is_wrapped_again() -> None:
    (finding,) = bracket_findings([_unit("0", "「あいう」", "啊咿嗚")], "livemaker")
    assert finding.fixes == {"pylm:text:a.lsb:5:0": "「啊咿嗚」"}


def test_neighbouring_translations_that_changed_places_are_swapped_back() -> None:
    units = [_unit("0", "「あ」", "（嗯）", line=5), _unit("0", "（う）", "「啊」", line=6)]
    first, second = bracket_findings(units, "livemaker")
    assert first.kind == second.kind == "swapped-translations"
    shown = [found.params["fixed"] for f in (first, second) for _, found in f.findings]
    assert shown == ["「啊」", "（嗯）"]
    swap = {"pylm:text:a.lsb:5:0": "「啊」", "pylm:text:a.lsb:6:0": "（嗯）"}
    assert first.fixes == second.fixes == swap


def test_a_difference_no_rule_can_settle_is_reported_without_a_fix() -> None:
    (finding,) = bracket_findings([_unit("0", "「あ」（い）", "（啊」")], "livemaker")
    assert finding.kind == "missing-or-extra"
    assert finding.fixes == {}


def test_inner_quotes_alone_are_the_translators_call() -> None:
    assert bracket_findings([_unit("0", "「『あ』だ」", "「是啊」")], "livemaker") == []
    assert bracket_findings([_unit("0", "「あ」『い』", "「啊」「咿」")], "livemaker") == []


def test_a_variables_name_and_half_width_parentheses_are_read_as_the_reader_sees_them() -> None:
    unit = _unit("0", "<VAR 【名前】>（あ）", "<VAR 【名前】>(啊)")
    assert bracket_findings([unit], "livemaker") == []


def test_a_message_with_a_piece_left_to_translate_is_not_compared() -> None:
    units = [_unit("0", "「あ", "「啊"), _unit("1", "い」", "")]
    assert bracket_findings(units, "livemaker") == []


def test_other_engines_compare_each_unit_on_its_own() -> None:
    units = [
        TextUnit(id="a:5:0", source="「あ", target="「啊", status=Status.MT),
        TextUnit(id="a:5:1", source="い」", target="咿", status=Status.MT),
    ]
    (finding,) = bracket_findings(units, "renpy")
    assert finding.unit_ids == ("a:5:1",)
