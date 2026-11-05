"""Ruby string literals in game scripts: found, decoded, encoded back and classified."""

import pytest

from bokucho.engines.ruby_literals import (
    Rule,
    UnreadableScript,
    compared,
    encode,
    literals,
    placeholder_mismatch,
    rule,
)


def _texts(source):
    return [literal.text for literal in literals(source)]


def test_both_quote_kinds_are_found_with_their_place():
    source = "a = \"Buy\"\nb = 'Sell'\n"
    found = literals(source)
    assert [(lit.text, lit.quote, lit.line, lit.column) for lit in found] == [
        ("Buy", '"', 1, 4), ("Sell", "'", 2, 4),
    ]
    assert source[found[0].start : found[0].end] == '"Buy"'


def test_escapes_decode_as_the_player_sees_them():
    assert _texts(r'"a\nb\"c\\"') == ['a\nb"c\\']
    assert _texts(r"'it\'s \n'") == ["it's \\n"]


def test_interpolation_stays_in_the_text_and_inner_literals_are_not_listed():
    assert _texts('x = "HP #{a ? "多" : "少"} 點"') == ['HP #{a ? "多" : "少"} 點']


def test_comments_and_end_hide_literals():
    assert _texts('# "no"\n=begin\n"no"\n=end\nx = "yes"\n__END__\n"no"\n') == ["yes"]


def test_skipped_forms_are_never_listed_and_do_not_derail():
    source = '%w(a "b)\nc = ?"\nh = <<EOS\n"in a heredoc\nEOS\nq = $"\ny = "after"\n'
    assert _texts(source) == ["after"]


def test_a_quote_inside_a_regexp_does_not_derail_the_rest():
    source = 'case x\nwhen /"/ then 1\nend\nok = (y =~ /\'/)\nz = "after"\n'
    assert _texts(source) == ["after"]


def test_division_is_not_a_regexp():
    assert _texts('w = width / 2\ns = "ok"\nh = height / 3\n') == ["ok"]


def test_an_unclosed_literal_is_unreadable():
    with pytest.raises(UnreadableScript):
        literals('x = "abc\n')


def test_unbalanced_brackets_are_unreadable():
    with pytest.raises(UnreadableScript):
        literals('x = foo("a"\n')


@pytest.mark.parametrize(
    "text", ["Buy", 'say "hi"\n', "100%", "back\\slash", "HP #{hp}", "#@x", "#$y", "a\tb"]
)
def test_encode_round_trips_through_literals(text):
    assert _texts(f'x = "{encode(text, chr(34))}"') == [text]
    if "#{" not in text and "\n" not in text and "\t" not in text:
        assert _texts(f"x = '{encode(text, chr(39))}'") == [text]


@pytest.mark.parametrize(("source", "target", "expected"), [
    ("%sが出現！", "%s出現了！", ([], [])),
    ("%sが出現！", "出現了！", (["%s"], [])),
    ("Lv", "等級%", ([], [])),
    ("%d個", "%d%個", ([], ["%"])),
    ("%d個", "%d %s個", ([], ["%s"])),
    ("#{name}の剣", "#{name}之劍", ([], [])),
    ("#{name}の剣", "#{nam}之劍", (["#{name}"], ["#{nam}"])),
    ("A\nB", "AB", (["\\n"], [])),
    ("10% damage", "傷害10%", ([], [])),
    ("100% off", "全部免費", ([], [])),
])
def test_placeholders(source, target, expected):
    assert placeholder_mismatch(source, target) == expected


ASSETS = frozenset({"A1 決定"})


@pytest.mark.parametrize(("source", "text", "expected"), [
    ('self.contents.draw_text(4, 0, 120, 32, "Play Time")', "Play Time", Rule.DISPLAY),
    ('@commands = ["Buy", "Sell", "Exit"]', "Buy", Rule.DISPLAY),
    ('s1 = "New Game"', "New Game", Rule.DISPLAY),
    (
        'super("Which file would you like to save to?")',
        "Which file would you like to save to?",
        Rule.DISPLAY,
    ),
    ('self.contents.draw_text(320, 160, 96, 32, "equipment")', "equipment", Rule.DISPLAY),
    ('module Vocab\n  ShopBuy = "Buy"\nend', "Buy", Rule.DISPLAY),
    ('self.bitmap.font.name = "Arial"', "Arial", Rule.CODE),
    ('  FONT_NAME = ["ＭＳ Ｐ明朝", "HGP明朝E"]', "ＭＳ Ｐ明朝", Rule.CODE),
    ('@font_name = "ＭＳ ゴシック"', "ＭＳ ゴシック", Rule.CODE),
    ('sgnt = "\\x89PNG\\r\\n\\x1a\\n"', "\x89PNG\r\n\x1a\n", Rule.SKIP),
    ('mark = "\\x01Data"', "\x01Data", Rule.SKIP),
    ('s1 = "Line one\\nLine two\\tend"', "Line one\nLine two\tend", Rule.DISPLAY),
    ('file = File.open(filename, "wb")', "wb", Rule.CODE),
    ('$data_system.sounds[1].name = "A1 決定"', "A1 決定", Rule.CODE),
    ('@filename = "Save#{@file_index + 1}.rxdata"', "Save#{@file_index + 1}.rxdata", Rule.SKIP),
    ('Audio.se_play("Audio/SE/" + name)', "Audio/SE/", Rule.SKIP),
    ('t = sprintf("%02d:%02d:%02d", h, m, s)', "%02d:%02d:%02d", Rule.SKIP),
    ('TABLE = ["A", "B", "C"]', "A", Rule.SKIP),
    ('text1 = "C (Enter) : ON / OFF"', "C (Enter) : ON / OFF", Rule.OTHER),
    ('name = "File#{@file_index + 1}"', "File#{@file_index + 1}", Rule.OTHER),
    ('x = "勇者の剣"', "勇者の剣", Rule.OTHER),
    ('["SE_name",75,50],', "SE_name", Rule.OTHER),
    ('CORE_SCRIPTS = ["Alpha", "Beta"]', "Beta", Rule.OTHER),
    (':list => ["Mode_A", "Mode_B"]', "Mode_B", Rule.OTHER),
    ('menu = ["購入", "売却"]', "購入", Rule.OTHER),
])
def test_each_measured_case_follows_its_rule(source, text, expected):
    [literal] = [lit for lit in literals(source) if lit.text == text][:1]
    assert rule(source, literal, frozenset(), ASSETS) is expected


def test_a_compared_word_is_a_key_wherever_it_appears():
    source = 'self.damage = "Miss"\nhit = (target.damage != "Miss")\n'
    found = literals(source)
    keys = frozenset(lit.text for lit in found if compared(source, lit))
    assert keys == {"Miss"}
    assert [rule(source, lit, keys, ASSETS) for lit in found] == [Rule.KEY, Rule.KEY]


def test_the_compared_forms():
    source = 'case a\nwhen "x", "y"\nend\nh = {"k" => 1}\nb.include?("i")\ns.sub("pat", "rep")\n'
    found = {lit.text: compared(source, lit) for lit in literals(source)}
    assert found == {"x": True, "y": True, "k": True, "i": True, "pat": True, "rep": False}


@pytest.mark.parametrize("source", [
    'n = a.index("火")\n',
    'ok = @t.key?("風")\n',
    'ok = h.has_key?("k")\n',
    'v = CONFIG.fetch("雷")\n',
    'ok = s.member?("m")\n',
])
def test_a_looked_up_name_is_compared(source):
    [literal] = literals(source)
    assert compared(source, literal)


def test_only_the_first_argument_of_fetch_is_compared():
    source = 'v = CONFIG.fetch("k", "default")\n'
    assert {lit.text: compared(source, lit) for lit in literals(source)} == {
        "k": True,
        "default": False,
    }


def test_a_fetched_key_is_a_key_wherever_it_appears():
    source = 'x = CONFIG.fetch("雷")\ndraw_text(0, 0, 9, 9, "雷")\n'
    found = literals(source)
    keys = frozenset(lit.text for lit in found if compared(source, lit))
    assert [rule(source, lit, keys, ASSETS) for lit in found] == [Rule.KEY, Rule.KEY]


@pytest.mark.parametrize("source", [
    '$imported["A_Script"] = true\n',
    'x = @@v["k"]\n',
    'y = args["mode"]\n',
    'z = f(x)["k"]\n',
    'w = a[0]["k"]\n',
])
def test_an_indexed_string_is_compared(source):
    [literal] = literals(source)
    assert compared(source, literal)


@pytest.mark.parametrize("source", [
    't = ["a", "b"]\n',
    'foo(["a"])\n',
    ':list => ["a", "b"]\n',
    'x = y ["a"]\n',
])
def test_an_array_element_is_not_compared(source):
    assert not any(compared(source, literal) for literal in literals(source))


def test_an_index_key_is_a_key_wherever_it_appears():
    source = '$imported["A_Script"] = true\nif $imported["A_Script"]\n'
    found = literals(source)
    keys = frozenset(lit.text for lit in found if compared(source, lit))
    assert [rule(source, lit, keys, ASSETS) for lit in found] == [Rule.KEY, Rule.KEY]


@pytest.mark.parametrize(("source", "expected"), [
    ('@text << "abc"\n', ["abc"]),
    ('s = "%d" % [n]\n', ["%d"]),
    ('t = a ? "x" : "y"\n', ["x", "y"]),
    ('x /= 2\ny = "ok"\n', ["ok"]),
    ('w = @x /2\ns = "ok"\n', ["ok"]),
    ('@a <<"q"\n', ["q"]),
    ('x = a ?"x" : "y"\nb = "#1"\n', ["x", "y", "#1"]),
])
def test_an_operator_does_not_open_a_skipped_form(source, expected):
    assert _texts(source) == expected


def test_a_regexp_left_open_on_its_line_is_unreadable():
    with pytest.raises(UnreadableScript):
        literals('x = /ab\ncd/\ny = "z"\n')


@pytest.mark.parametrize(("source", "expected"), [
    ('x = "a\\\r\nb"\r\n', ["ab"]),
    ("y = 'c\\\r\nd'\r\n", ["c\\\nd"]),
    ("x = \"a\r\nb\"\r\ny = 'c\r\nd'\r\n", ["a\nb", "c\nd"]),
])
def test_a_crlf_script_reads_as_ruby_folds_it(source, expected):
    assert _texts(source) == expected


def test_a_crlf_script_keeps_line_and_column():
    found = literals('a = 1\r\n  b = "x"\r\nc = "y\r\nz" + "w"\r\n')
    assert [(lit.text, lit.line, lit.column) for lit in found] == [
        ("x", 2, 6), ("y\nz", 3, 4), ("w", 4, 5),
    ]


def test_an_escaped_interpolation_is_never_extracted():
    source = 'x = "Use \\#{name} here"\n'
    [literal] = literals(source)
    assert literal.text == "Use #{name} here"
    assert rule(source, literal, frozenset(), ASSETS) is Rule.SKIP


def test_an_indented_heredoc_ends_at_its_terminator_and_its_line_goes_on():
    source = 'x = foo(<<-EOS, "tail")\n  "body\n  EOS\ny = "after"\n'
    assert _texts(source) == ["tail", "after"]


@pytest.mark.parametrize(("raw", "text"), [("HP #@hp", "HP #{@hp}"), ("#$game", "#{$game}")])
def test_short_interpolation_reads_as_its_braced_form_and_round_trips(raw, text):
    assert _texts(f'x = "{raw}"') == [text]
    assert _texts(f'x = "{encode(text, chr(34))}"') == [text]


@pytest.mark.parametrize("source", ["x = '#@x'", 'x = "\\#@x"'])
def test_short_interpolation_is_plain_text_in_single_quotes_or_escaped(source):
    assert _texts(source) == ["#@x"]


def test_a_backslash_newline_in_double_quotes_joins_the_lines():
    assert _texts('x = "a\\\nb"\ny = \'c\\\nd\'\n') == ["ab", "c\\\nd"]
