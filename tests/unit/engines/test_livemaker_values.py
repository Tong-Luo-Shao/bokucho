"""The values a LiveMaker game shows, followed from where they are assigned."""

from __future__ import annotations

import string

import pytest

pytest.importorskip("livemaker")

from livemaker.lsb.core import OpeData, OpeDataType, OpeFuncType, Param, ParamType  # noqa: E402

from bokucho.engines.livemaker_values import (  # noqa: E402
    analyse,
    assembled,
    inconsistencies,
    mismatches,
    value_id,
)
from bokucho.findings import TEMPLATES  # noqa: E402

A, B, C = "00000001.lsb", "00000002.lsb", "00000003.lsb"


def _values(*scripts):
    return analyse({script.lsb.call_name: script.lsb for script in scripts})


def _words(*scripts) -> dict[str, set[str]]:
    return _values(*scripts).words()


def test_a_statement_is_compiled_as_livemaker_prints_it(livemaker_script):
    for text in [
        '装備名[6] == "旅人の服"',
        'JPos("剣", 装備名[0]) >= 1',
        'アイテム名 = 種類名 ++ "外す"',
        'アイテム名 = StringReplace(アイテム名, 種類名, "", 1, 1)',
        '(選択値 == "外す" & 装備名[0] == "ダガー")',
        "n = Exists(設定[i])",
    ]:
        assert str(livemaker_script.statement(text)) == text


def test_a_line_a_menu_shows_is_translatable(livemaker_script):
    script = livemaker_script(A)
    script.runs('設定_バックアップ = "自動保存　【ON】　OFF"')
    script.runs("AddArray(_tmp, 設定_バックアップ)")
    assert _words(script) == {"自動保存　【ON】　OFF": {"設定_バックアップ"}}


def test_an_element_shown_in_text_is_translatable(livemaker_script):
    script = livemaker_script(A)
    script.runs('装備名[0] = "ダガー"')
    script.shows_in_text("装備名[0]")
    assert _words(script) == {"ダガー": {"装備名"}}


def test_a_value_any_other_function_uses_is_not(livemaker_script):
    script = livemaker_script(A)
    script.runs('名前 = "勇者"')
    script.runs("AddArray(_tmp, 名前)")
    script.runs("n = Exists(名前)")
    values = _values(script)
    assert values.words() == {}
    assert values.excluded() == {"勇者": {"名前"}}


def test_an_element_is_a_place_of_its_own(livemaker_script):
    script = livemaker_script(A)
    script.runs('設定[0] = "クリティカル！"')
    script.runs('設定[35] = "立ち絵.gal"')
    script.runs("AddArray(_tmp, 設定[0])")
    script.runs("n = Exists(設定[35])")
    assert _words(script) == {"クリティカル！": {"設定"}}


def test_an_array_a_variable_index_keys_is_keyed_in_every_element(livemaker_script):
    script = livemaker_script(A)
    script.runs('設定[0] = "クリティカル！"')
    script.runs("AddArray(_tmp, 設定[0])")
    script.runs("n = Exists(設定[i])")
    assert _words(script) == {}


def test_a_variable_another_script_declares_its_own_is_not_this_one(livemaker_script):
    keeper = livemaker_script(A)
    keeper.declares_own("名前", '"鍵"')
    keeper.runs("n = Exists(名前)")
    shower = livemaker_script(B)
    shower.runs('名前 = "勇者"')
    shower.runs("AddArray(_tmp, 名前)")
    assert _words(keeper, shower) == {"勇者": {"名前"}}


def test_a_parameter_is_the_called_scripts_own(livemaker_script):
    caller = livemaker_script(A)
    caller.calls(B, '"勇者"')
    callee = livemaker_script(B)
    callee.runs("AddArray(_tmp, @ParamStr[0])")
    assert _words(caller, callee) == {"勇者": {"@ParamStr"}}


def test_a_parameter_of_a_script_not_in_the_game_is_a_key(livemaker_script):
    script = livemaker_script(A)
    script.runs('名前 = "勇者"')
    script.runs("AddArray(_tmp, 名前)")
    script.calls("00000009.lsb", "名前")
    assert _words(script) == {}


def test_a_caption_is_shown_and_any_other_property_is_a_key(livemaker_script):
    script = livemaker_script(A)
    script.runs('場所名前 = "北の丘"')
    script.sets(50, "場所名前")
    script.runs('背景 = "森の背景"')
    script.sets(2, "背景")
    assert _words(script) == {"北の丘": {"場所名前"}}


def test_the_text_a_label_or_button_is_made_with_is_shown(livemaker_script):
    """The save screen's prompt and buttons."""
    from livemaker.lsb.command import Button, Caption

    caller, dialog = livemaker_script(A), livemaker_script(B)
    caller.calls(B, '"番にセーブします"')
    dialog.add(
        Caption,
        Name=dialog.statement('"プロンプト"'),
        PR_TEXT=dialog.statement("@ParamStr[0]"),
        PR_FONTNAME=dialog.statement('"ＭＳ Ｐゴシック"'),
    )
    dialog.add(
        Button,
        Name=dialog.statement('"取消ボタン"'),
        PR_TEXT=dialog.statement('"キャンセル"'),
    )
    assert _words(caller, dialog) == {"番にセーブします": {"@ParamStr"}, "キャンセル": set()}


def _parser(*entries):
    """A statement as LiveMaker compiles it, for what the test compiler cannot write."""
    from livemaker.lsb.core import LiveParser

    return LiveParser(
        entries=[
            OpeData(kind, name, func[0] if func else None, ops)
            for kind, name, ops, *func in entries
        ]
    )


def _var(name: str) -> Param:
    return Param(name, ParamType.Var)


def _int(value: int) -> Param:
    return Param(value, ParamType.Int)


def _titled_dialog(script):
    """The save-memo dialog's first lines: its title is its first parameter."""
    from livemaker.lsb.command import Caption

    script.add(
        Caption, Name=script.statement('"見出し"'), PR_TEXT=script.statement("@ParamStr[0]")
    )


def test_a_parameter_read_as_a_number_is_still_shown(livemaker_script):
    caller, dialog = livemaker_script(A), livemaker_script(B)
    caller.calls(B, '"番にセーブします"')
    _titled_dialog(dialog)
    dialog.checks("Int(@ParamStr[0]) == 13")
    assert _words(caller, dialog) == {"番にセーブします": {"@ParamStr"}}


def _reads_from_the_end(script, back: int) -> None:
    """x = @ParamStr[@ParamCount - back], and x used as a key."""
    from livemaker.lsb.command import Calc

    script.add(
        Calc,
        Calc=_parser(
            (OpeDataType.Minus, "____0", [_var("@ParamCount"), _int(back)]),
            (OpeDataType.DimTo, "____d_1", [_var("@ParamStr"), _var("____0")]),
            (OpeDataType.To, "x", [_var("____d_1")]),
        ),
    )
    script.sets(2, "x")


def test_a_parameter_counted_from_the_end_keys_only_that_one(livemaker_script):
    caller, dialog = livemaker_script(A), livemaker_script(B)
    caller.calls(B, '"番にセーブします"', '"中央"')
    _titled_dialog(dialog)
    _reads_from_the_end(dialog, 1)
    assert _words(caller, dialog) == {"番にセーブします": {"@ParamStr"}}


def test_a_call_whose_last_parameter_is_the_first_keys_it(livemaker_script):
    caller, dialog = livemaker_script(A), livemaker_script(B)
    caller.calls(B, '"番にセーブします"')
    _titled_dialog(dialog)
    _reads_from_the_end(dialog, 1)
    assert _words(caller, dialog) == {}


def _keys_parameters_from(script, start: int, *, own: bool = True, body: str = "") -> None:
    """WhileInit i = start; While i < @ParamCount: Exists(@ParamStr[i]); WhileLoop i = i + 1."""
    from livemaker.lsb.command import Calc, While, WhileInit, WhileLoop

    if own:
        script.declares_own("i", "0")
    script.add(WhileInit, Calc=_parser((OpeDataType.To, "i", [_int(start)])))
    loop = len(script.lsb.commands)
    test = script.add(
        While,
        Calc=_parser(
            (OpeDataType.Small, "____0", [_var("i"), _var("@ParamCount")]),
            (OpeDataType.To, "____arg", [_var("____0")]),
        ),
    )
    script.add(
        Calc,
        Calc=_parser(
            (OpeDataType.DimTo, "____d_0", [_var("@ParamStr"), _var("i")]),
            (OpeDataType.To, "____1", [_var("____d_0")]),
            (OpeDataType.Func, "____2", [_var("____1")], OpeFuncType.Exists),
            (OpeDataType.To, "____arg", [_var("____2")]),
        ),
    )
    if body:
        script.calls(body)
    script.add(
        WhileLoop,
        Calc=_parser(
            (OpeDataType.Plus, "____0", [_var("i"), _int(1)]),
            (OpeDataType.To, "i", [_var("____0")]),
        ),
        Start=loop,
    )
    test.args["End"] = len(script.lsb.commands)


@pytest.mark.parametrize(("start", "shown"), [(2, True), (0, False)])
def test_a_loop_over_parameters_keys_only_those_it_counts_over(livemaker_script, start, shown):
    caller, dialog = livemaker_script(A), livemaker_script(B)
    caller.calls(B, '"番にセーブします"', '"1"', '"メモ"')
    _titled_dialog(dialog)
    _keys_parameters_from(dialog, start)
    expected = {"番にセーブします": {"@ParamStr"}} if shown else {}
    assert _words(caller, dialog) == expected


def test_a_loop_counter_another_script_may_set_has_no_floor(livemaker_script):
    caller, dialog, other = livemaker_script(A), livemaker_script(B), livemaker_script(C)
    caller.calls(B, '"番にセーブします"', '"1"', '"メモ"')
    _titled_dialog(dialog)
    _keys_parameters_from(dialog, 2, own=False, body=C)
    other.runs("i = 0")
    assert _words(caller, dialog, other) == {}


def test_a_value_split_into_an_array_and_joined_back_is_shown(livemaker_script):
    script = livemaker_script(A)
    script.runs('場所名前 = "北の丘"')
    script.runs("S = 場所名前")
    script.runs("StringToArray(S, SS)")
    script.sets(50, "ArrayToString(SS)")
    assert _words(script) == {"北の丘": {"場所名前"}}


def test_a_cut_compared_with_a_word_keys_what_it_was_cut_from(livemaker_script):
    script = livemaker_script(A)
    script.runs('名前 = "勇者様"')
    script.runs("AddArray(_tmp, 名前)")
    script.runs("頭 = JCopy(名前, 1, 2)")
    script.checks('頭 == "勇者"')
    assert _words(script) == {}


def test_a_value_compared_with_a_built_string_is_keyed(livemaker_script):
    script = livemaker_script(A)
    script.runs('装備名[0] = "ダガー"')
    script.runs("AddArray(_tmp, 装備名[0])")
    script.checks('装備名[0] == "ダガー" ++ 接尾')
    assert _words(script) == {}


def test_a_word_only_checked_or_searched_on_a_shown_value_is_translatable(livemaker_script):
    script = livemaker_script(A)
    script.runs('アイテム名 = "革の靴【手足】"')
    script.runs("AddArray(_tmp, アイテム名)")
    script.runs('位置 = JPos("【", アイテム名)')
    script.runs("JDelete(アイテム名, 位置)")
    script.runs("装備名[7] = アイテム名")
    script.checks('装備名[7] == "革の靴"')
    values = _values(script)
    assert values.words() == {
        "革の靴【手足】": {"アイテム名"},
        "【": {"アイテム名"},
        "革の靴": {"装備名"},
    }
    assert len(values.cuts) == 1


def test_a_choice_a_menu_offers_itself_is_not_a_value(livemaker_script):
    script = livemaker_script(A)
    script.runs('AddArray(_tmp, "ロード")')
    assert _words(script) == {}


def test_a_choice_built_around_a_number_is_a_value(livemaker_script):
    script = livemaker_script(A)
    script.runs('AddArray(_tmp, "開ける(古い鍵" ++ 個数 ++ ")")')
    assert _words(script) == {"開ける(古い鍵": {"_tmp"}}


def test_a_built_choice_the_pick_is_checked_against_stays(livemaker_script):
    script = livemaker_script(A)
    script.runs('AddArray(_tmp, "料金(" ++ 宿代 ++ " G)")')
    script.checks('選択値 == "料金(" ++ 宿代 ++ " G)"')
    assert _words(script) == {}


def test_a_word_a_menu_may_show_from_a_variable_is_offered(livemaker_script):
    script = livemaker_script(A)
    script.runs('装備名[0] = "ダガー"')
    script.runs("AddArray(_tmp, 装備名[0])")
    script.runs('場所名前 = "北の丘"')
    script.sets(50, "場所名前")
    assert _values(script).offered() == {"ダガー"}


def test_a_literal_a_key_also_reads_is_never_written(livemaker_script):
    script = livemaker_script(A)
    script.runs("AddArray(_tmp, 名前)")
    command = script.runs('名前 = "勇者"')
    command.args["Calc"].entries.insert(
        1,
        OpeData(
            OpeDataType.Func, "____9", OpeFuncType.Exists, [Param("____0", ParamType.Var)]
        ),
    )
    values = _values(script)
    (site,) = values.sites
    assert values.translatable(site)
    assert id(site.operand) in values.fixed


def _problems(translations: dict[str, str], *scripts) -> list[str]:
    return inconsistencies(_values(*scripts), lambda word: translations.get(word, word))


def _findings(translations: dict[str, str], *scripts):
    found = mismatches(_values(*scripts), lambda word: translations.get(word, word))
    return [finding for finding, _, _ in found]


@pytest.fixture
def sword(livemaker_script):
    script = livemaker_script(A)
    script.runs('装備名[0] = "銀の長剣"')
    script.runs("AddArray(_tmp, 装備名[0])")
    script.checks('JPos("剣", 装備名[0]) >= 1')
    return script


def test_a_needle_translated_as_its_haystacks_are_is_consistent(sword):
    assert _problems({"銀の長剣": "銀長劍", "剣": "劍"}, sword) == []


def test_a_needle_a_translated_haystack_no_longer_holds_is_named(sword):
    (problem,) = _problems({"銀の長剣": "銀長劍", "剣": "刀"}, sword)
    assert "searches" in problem
    assert value_id("剣") in problem and value_id("銀の長剣") in problem


def test_a_needle_left_untranslated_stands_for_itself(sword):
    (problem,) = _problems({"銀の長剣": "銀長劍"}, sword)
    assert "searches" in problem


@pytest.fixture
def boots(livemaker_script):
    script = livemaker_script(A)
    script.runs('アイテム名 = "革の靴【手足】"')
    script.runs("AddArray(_tmp, アイテム名)")
    script.runs('位置 = JPos("【", アイテム名)')
    script.runs("JDelete(アイテム名, 位置)")
    script.runs("装備名[7] = アイテム名")
    script.checks('装備名[7] == "革の靴"')
    return script


def test_a_cut_translated_alike_is_consistent(boots):
    assert _problems({"革の靴【手足】": "皮靴【手腳】", "革の靴": "皮靴"}, boots) == []


def test_a_cut_whose_translation_is_not_the_translated_word_is_named(boots):
    problems = _problems({"革の靴【手足】": "皮靴 【手腳】", "革の靴": "皮靴"}, boots)
    assert any("cuts" in p and value_id("革の靴") in p for p in problems)


@pytest.fixture
def dagger(livemaker_script):
    script = livemaker_script(A)
    script.runs('アイテム名 = "ダガー【武器】"')
    script.runs("AddArray(_tmp, アイテム名)")
    script.runs('種類名 = "【武器】"')
    script.runs("AddArray(_tmp, 種類名)")
    script.runs('アイテム名 = StringReplace(アイテム名, 種類名, "", 1, 1)')
    script.runs("装備名[0] = アイテム名")
    script.checks('装備名[0] == "ダガー"')
    return script


def test_a_replacement_translated_alike_is_consistent(dagger):
    translations = {"ダガー【武器】": "匕首【兵器】", "【武器】": "【兵器】", "ダガー": "匕首"}
    assert _problems(translations, dagger) == []


def test_a_replacement_whose_translation_is_not_the_translated_word_is_named(dagger):
    problems = _problems({"ダガー【武器】": "匕首【兵器】", "ダガー": "匕首"}, dagger)
    assert any("replaces" in p and value_id("【武器】") in p for p in problems)


@pytest.fixture
def unequip(livemaker_script):
    script = livemaker_script(A)
    script.runs('種類名 = "【服】"')
    script.runs("AddArray(_tmp, 種類名)")
    script.runs('アイテム名 = 種類名 ++ "外す"')
    script.runs("AddArray(_tmp, アイテム名)")
    script.checks('アイテム名 == "【服】外す"')
    return script


def test_a_join_translated_alike_is_consistent(unequip):
    translations = {"【服】": "【服裝】", "外す": "卸下", "【服】外す": "【服裝】卸下"}
    assert _problems(translations, unequip) == []


def test_a_join_whose_translation_is_not_the_translated_word_is_named(unequip):
    translations = {"【服】": "【服裝】", "外す": "卸下", "【服】外す": "【服】卸下"}
    (problem,) = _problems(translations, unequip)
    assert "joins" in problem and value_id("【服】外す") in problem


@pytest.fixture
def blades(livemaker_script):
    script = livemaker_script(A)
    script.runs('装備名[0] = "ダガー"')
    script.runs('装備名[0] = "銀の長剣"')
    script.runs('装備名[0] = "オークの杖"')
    script.runs("AddArray(_tmp, 装備名[0])")
    script.checks('(JPos("剣", 装備名[0]) >= 1 | JPos("ダガー", 装備名[0]) >= 1)')
    return script


def test_searches_any_of_which_tells_a_kind_keep_it_when_each_value_keeps_its_kind(blades):
    translations = {
        "ダガー": "短劍",
        "銀の長剣": "銀長劍",
        "剣": "劍",
        "オークの杖": "橡木法杖",
    }
    assert _problems(translations, blades) == []


def test_searches_any_of_which_tells_a_kind_name_a_value_that_changes_kind(blades):
    (problem,) = _problems({"オークの杖": "橡木劍杖", "剣": "劍"}, blades)
    assert "searches" in problem
    assert value_id("オークの杖") in problem and value_id("剣") in problem


def test_searches_asked_anything_else_are_checked_one_by_one(livemaker_script):
    script = livemaker_script(A)
    script.runs('装備名[0] = "ダガー"')
    script.runs("AddArray(_tmp, 装備名[0])")
    script.checks('(JPos("剣", 装備名[0]) >= 1 & JPos("ダガー", 装備名[0]) >= 1)')
    (problem,) = _problems({"ダガー": "短劍", "剣": "劍"}, script)
    assert "searches" in problem and value_id("剣") in problem


def _assembled(translations: dict[str, str], *scripts, fixable=lambda word: True):
    return assembled(_values(*scripts), lambda word: translations.get(word, word), fixable)


def test_a_join_is_assembled_from_the_translations_of_its_pieces(unequip):
    translations = {"【服】": "【服裝】", "外す": "卸下", "【服】外す": "【服】卸下"}
    fixes = _assembled(translations, unequip)
    assert fixes == {"【服】外す": "【服裝】卸下"}
    assert _problems({**translations, **fixes}, unequip) == []


def test_a_cut_value_starts_with_the_translation_of_what_it_is_cut_into(boots):
    translations = {"革の靴【手足】": "皮靴 【手腳】", "革の靴": "皮靴"}
    fixes = _assembled(translations, boots)
    assert fixes == {"革の靴【手足】": "皮靴【手腳】"}
    assert _problems({**translations, **fixes}, boots) == []


def test_a_value_a_word_is_replaced_in_holds_that_words_translation(dagger):
    translations = {"ダガー【武器】": "匕首【兵器】", "【武器】": "【武器類】", "ダガー": "匕首"}
    fixes = _assembled(translations, dagger)
    assert fixes == {"ダガー【武器】": "匕首【武器類】"}
    assert _problems({**translations, **fixes}, dagger) == []


@pytest.fixture
def clothes(livemaker_script):
    script = livemaker_script(A)
    script.runs('装備名[0] = "布の服【服】"')
    script.runs('基本名 = "布の服"')
    script.runs("AddArray(_tmp, 装備名[0])")
    script.runs("AddArray(_tmp, 基本名)")
    script.checks('JPos("【服】", 装備名[0]) >= 1')
    return script


def test_a_searched_value_of_two_words_is_their_translations_side_by_side(clothes):
    translations = {"布の服【服】": "布衣【服裝】", "布の服": "布衣", "【服】": "【衣服】"}
    fixes = _assembled(translations, clothes)
    assert fixes == {"布の服【服】": "布衣【衣服】"}
    assert _problems({**translations, **fixes}, clothes) == []


def test_a_value_that_may_not_be_changed_is_left_reported(clothes):
    translations = {"布の服【服】": "布衣【服裝】", "布の服": "布衣", "【服】": "【衣服】"}
    assert _assembled(translations, clothes, fixable=lambda word: False) == {}


def test_a_local_a_called_script_reads_is_the_callers(livemaker_script):
    caller = livemaker_script(A)
    caller.declares_own("名前", '"勇者"')
    caller.runs("AddArray(_tmp, 名前)")
    caller.calls(B, "1")
    callee = livemaker_script(B)
    callee.runs("n = Exists(名前)")
    assert _words(caller, callee) == {}


def test_a_local_declared_only_if_it_does_not_exist_is_not_the_scripts_own(livemaker_script):
    keeper = livemaker_script(A)
    keeper.runs('n = VarExists("名前")')
    keeper.declares_own("名前", '"鍵"')
    keeper.runs("n = Exists(名前)")
    shower = livemaker_script(B)
    shower.runs('名前 = "勇者"')
    shower.runs("AddArray(_tmp, 名前)")
    assert _words(keeper, shower) == {}


def test_a_variable_declared_with_scope_3_is_global(livemaker_script):
    from livemaker.lsb.command import VarNew

    keeper = livemaker_script(A)
    keeper.add(VarNew, Name="名前", InitVal=keeper.statement('"鍵"'), Scope=3)
    keeper.runs("n = Exists(名前)")
    shower = livemaker_script(B)
    shower.runs('名前 = "勇者"')
    shower.runs("AddArray(_tmp, 名前)")
    assert _words(keeper, shower) == {}


def test_each_kind_of_inconsistency_is_named_by_a_code_with_the_values_of_its_sentence(
    sword, blades, boots, dagger, unequip
):
    kinds = {
        "values_search_found": _findings({"銀の長剣": "銀長劍", "剣": "刀"}, sword),
        "values_search_any_missing": _findings({"オークの杖": "橡木劍杖", "剣": "劍"}, blades),
        "values_cut": _findings({"革の靴【手足】": "皮靴 【手腳】", "革の靴": "皮靴"}, boots),
        "values_replace": _findings({"ダガー【武器】": "匕首【兵器】", "ダガー": "匕首"}, dagger),
        "values_join": _findings(
            {"【服】": "【服裝】", "外す": "卸下", "【服】外す": "【服】卸下"}, unequip
        ),
    }

    for code, findings in kinds.items():
        assert code in {finding.code for finding in findings}
        for finding in findings:
            assert finding.check == "values"
            assert set(finding.params) == {
                name for _, name, _, _ in string.Formatter().parse(TEMPLATES[finding.code]) if name
            }
