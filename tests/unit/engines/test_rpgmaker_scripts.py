"""The Scripts file codec: names and Ruby sources read out, changed sources spliced back."""

import zlib

import pytest

pytest.importorskip("rubymarshal")

from bokucho.application.contracts import ProgressLevel  # noqa: E402
from bokucho.engines.rpgmaker import RpgMakerAdapter  # noqa: E402
from bokucho.engines.rpgmaker_marshal import dump_marshal  # noqa: E402
from bokucho.engines.rpgmaker_scripts import (  # noqa: E402
    ScriptRefused,
    dump_scripts,
    load_scripts,
    rewrite_scripts,
    script_texts,
    with_font_loader,
)
from bokucho.model import Status  # noqa: E402

SOURCES = [("Scene_End", 's1 = "To Title"\n'), ("Main", 'print("Unable")\n')]


@pytest.mark.parametrize("build", ["xp_scripts", "ace_scripts"])
def test_scripts_read_as_names_and_sources(build, request):
    file = load_scripts(request.getfixturevalue(build)(SOURCES))
    assert [(entry.index, entry.name, entry.source) for entry in file.entries] == [
        (0, "Scene_End", 's1 = "To Title"\n'), (1, "Main", 'print("Unable")\n'),
    ]


@pytest.mark.parametrize("build", ["xp_scripts", "ace_scripts"])
def test_a_changed_script_is_the_only_bytes_that_change(build, request):
    blob = request.getfixturevalue(build)(SOURCES)
    file = load_scripts(blob)
    out = dump_scripts(file, {0: 's1 = "回到標題"\n'})
    code = file.entries[0].code
    assert out[: code.start] == blob[: code.start]
    assert out.endswith(blob[code.end :])
    sources = [entry.source for entry in load_scripts(out).entries]
    assert sources == ['s1 = "回到標題"\n', 'print("Unable")\n']


def test_unchanged_scripts_give_back_the_original_bytes(ace_scripts):
    blob = ace_scripts(SOURCES)
    assert dump_scripts(load_scripts(blob), {}) == blob


def test_a_source_equal_to_the_current_one_is_not_a_change(xp_scripts):
    blob = xp_scripts(SOURCES)
    assert dump_scripts(load_scripts(blob), {1: SOURCES[1][1]}) == blob


def test_an_index_that_is_not_in_the_file_is_refused(xp_scripts):
    with pytest.raises(ValueError, match="7"):
        dump_scripts(load_scripts(xp_scripts(SOURCES)), {7: "x = 1\n"})


def test_a_script_that_does_not_inflate_is_reported_and_the_rest_read():
    blob = dump_marshal([[0, b"Broken", b"not zlib"], [1, b"Main", zlib.compress(b'p("ok")\n')]])
    broken, main = load_scripts(blob).entries
    assert (broken.source, bool(broken.problem), main.source) == (None, True, 'p("ok")\n')


def test_a_script_that_is_not_utf8_is_reported():
    blob = dump_marshal([[0, b"Old", zlib.compress('p("あ")'.encode("cp932"))]])
    [entry] = load_scripts(blob).entries
    assert entry.source is None and "UTF-8" in entry.problem


def test_an_unreadable_script_keeps_its_bytes_when_another_is_changed():
    blob = dump_marshal([[0, b"Broken", b"not zlib"], [1, b"Main", zlib.compress(b'p("ok")\n')]])
    file = load_scripts(blob)
    out = dump_scripts(file, {1: 'p("好")\n'})
    broken, main = load_scripts(out).entries
    assert (bytes(broken.code), main.source) == (b"not zlib", 'p("好")\n')


def test_a_file_of_another_shape_is_refused():
    with pytest.raises(ValueError, match="0"):
        load_scripts(dump_marshal([[1, b"only a name"]]))


def test_a_file_that_is_not_an_array_is_refused():
    with pytest.raises(ValueError, match="array"):
        load_scripts(dump_marshal(5))


SCENE = (
    's1 = "To Title"\n'
    's2 = "シャットダウン"\n'
    'self.bitmap.font.name = "Arial"\n'
    '$data_system.sounds[1].name = "A1 決定"\n'
)
BATTLE = 'self.damage = "Miss"\nhit = (target.damage != "Miss")\ntext = "[Normal]"\n'
VOCAB = 'module Vocab\n  Emerge = "%sが出現！"\nend\n'


def _xp_game(tmp_path, scripts: bytes):
    game = tmp_path / "game"
    (game / "Data").mkdir(parents=True)
    (game / "Data" / "System.rxdata").write_bytes(dump_marshal(None))
    (game / "Data" / "Scripts.rxdata").write_bytes(scripts)
    (game / "Audio" / "SE").mkdir(parents=True)
    (game / "Audio" / "SE" / "A1 決定.ogg").write_bytes(b"ogg")
    return game


def test_script_text_is_extracted_by_the_rules(tmp_path, xp_scripts):
    units = RpgMakerAdapter().extract(
        _xp_game(tmp_path, xp_scripts([("Scene_End", SCENE), ("Battle", BATTLE)]))
    )
    found = {
        (unit.source, unit.engine_ref["address"]): (
            unit.status, unit.kind, unit.engine_ref["key"], unit.engine_ref.get("asset"),
        )
        for unit in units
    }
    assert found == {
        ("To Title", "s0:1:5"): (Status.UNTRANSLATED, "script", False, None),
        ("シャットダウン", "s0:2:5"): (Status.UNTRANSLATED, "script", False, None),
        ("Arial", "s0:3:24"): (Status.IGNORED, "script", False, None),
        ("A1 決定", "s0:4:30"): (Status.IGNORED, "script", False, "Audio/SE/A1 決定.ogg"),
        ("Miss", "s1:1:14"): (Status.IGNORED, "script", True, None),
        ("Miss", "s1:2:24"): (Status.IGNORED, "script", True, None),
        ("[Normal]", "s1:3:7"): (Status.IGNORED, "script", False, None),
    }
    assert {unit.id for unit in units} >= {"Scripts:s0:1:5"}
    assert next(unit for unit in units if unit.source == "To Title").context.scene == "Scene_End:1"


def test_a_translated_literal_is_written_and_everything_else_in_the_script_is_kept(
    tmp_path, xp_scripts
):
    game = _xp_game(tmp_path, xp_scripts([("Scene_End", SCENE)]))
    units = RpgMakerAdapter().extract(game)
    for unit in units:
        if unit.source == "To Title":
            unit.target = '回到"標題"\\'
    RpgMakerAdapter().inject(units, game, tmp_path / "out")
    [entry] = load_scripts((tmp_path / "out" / "Data" / "Scripts.rxdata").read_bytes()).entries
    assert entry.source == SCENE.replace('"To Title"', '"回到\\"標題\\"\\\\"')


def test_a_translation_that_loses_a_placeholder_is_refused_before_anything_is_written(
    tmp_path, xp_scripts
):
    game = _xp_game(tmp_path, xp_scripts([("Vocab", VOCAB)]))
    units = RpgMakerAdapter().extract(game)
    units[0].target = "出現了！"
    with pytest.raises(ScriptRefused, match=r"Vocab line 2"):
        RpgMakerAdapter().inject(units, game, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("*.*"))


def test_a_script_refusal_and_a_line_count_mismatch_are_named_together(
    tmp_path, ruby, xp_scripts
):
    game = _xp_game(tmp_path, xp_scripts([("Vocab", VOCAB)]))

    def command(code, *parameters):
        return ruby("RPG::EventCommand", code=code, indent=0, parameters=list(parameters))

    actor = ruby("RPG::Actor", id=1, name="ハロルド".encode())
    (game / "Data" / "Actors.rxdata").write_bytes(dump_marshal([None, actor]))
    talk = [command(101, "一行目".encode()), command(401, "二行目".encode()), command(0)]
    event = ruby("RPG::CommonEvent", id=1, list=talk)
    (game / "Data" / "CommonEvents.rxdata").write_bytes(dump_marshal([None, event]))
    units = RpgMakerAdapter().extract(game)
    targets = {"%sが出現！": "出現了！", "ハロルド": "哈羅德", "一行目\n二行目": "只有一行"}
    for unit in units:
        unit.target = targets[unit.source]
    with pytest.raises(ScriptRefused) as refused:
        RpgMakerAdapter().inject(units, game, tmp_path / "out")
    assert "Vocab line 2" in str(refused.value)
    assert "CommonEvents: ev1:c0" in str(refused.value)
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("*.*"))


def test_a_line_inserted_above_a_translated_literal_refuses_the_inject(tmp_path, xp_scripts):
    game = _xp_game(tmp_path, xp_scripts([("Scene_End", SCENE)]))
    units = RpgMakerAdapter().extract(game)
    for unit in units:
        if unit.source == "To Title":
            unit.target = "回到標題"
    moved = xp_scripts([("Scene_End", 's0 = "Hello"\n' + SCENE)])
    (game / "Data" / "Scripts.rxdata").write_bytes(moved)
    with pytest.raises(ScriptRefused, match=r"Scene_End line 1 \(s0:1:5\): the script changed"):
        RpgMakerAdapter().inject(units, game, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("*.*"))


def test_a_loose_sound_beside_an_archive_is_an_asset(tmp_path, build_archive, ace_scripts):
    game = tmp_path / "game"
    (game / "Audio" / "SE").mkdir(parents=True)
    (game / "Audio" / "SE" / "A1 決定.ogg").write_bytes(b"ogg")
    scripts = ace_scripts([("Sound", '@se = "A1 決定"\n')])
    build_archive(
        game / "Game.rgss3a",
        {"Data\\System.rvdata2": b"\x04\x080", "Data\\Scripts.rvdata2": scripts},
    )
    [unit] = RpgMakerAdapter(workspace=tmp_path / "unpacked").extract(game)
    assert (unit.status, unit.engine_ref.get("asset")) == (Status.IGNORED, "Audio/SE/A1 決定.ogg")


def test_a_script_the_reader_cannot_follow_costs_only_itself(tmp_path, xp_scripts):
    events = []
    game = _xp_game(tmp_path, xp_scripts([("Good", 's1 = "Fight"\n'), ("Bad", 'x = foo("a"\n')]))
    units = RpgMakerAdapter().extract(game, _Recorder(events))
    assert [unit.source for unit in units] == ["Fight"]
    assert any(
        "Bad" in (event.detail or "") for event in events if event.level is ProgressLevel.WARNING
    )


def test_ace_scripts_are_read_and_written(tmp_path, ace_scripts):
    game = tmp_path / "game"
    (game / "Data").mkdir(parents=True)
    (game / "Data" / "System.rvdata2").write_bytes(b"\x04\x080")
    (game / "Data" / "Scripts.rvdata2").write_bytes(
        ace_scripts([("Vocab", 'module Vocab\n  ShopBuy = "購入する"\nend\n')])
    )
    units = RpgMakerAdapter().extract(game)
    units[0].target = "購買"
    RpgMakerAdapter().inject(units, game, tmp_path / "out")
    [entry] = load_scripts((tmp_path / "out" / "Data" / "Scripts.rvdata2").read_bytes()).entries
    assert entry.source == 'module Vocab\n  ShopBuy = "購買"\nend\n'


@pytest.fixture
def menu(xp_scripts):
    """A Scripts file of one script, Menu, holding ``source``."""
    return lambda source: load_scripts(xp_scripts([("Menu", source)]))


@pytest.mark.parametrize(
    ("source", "address", "text", "target", "reason"),
    [
        ('s1 = "Name"\n', "s0:1:5", "Name", 'Name#{system("calc")}', "adds"),
        ('s1 = "Name"\n', "s0:9:5", "Name", "名前", "no literal"),
        ('s1 = "Name"\n', "s0:1:5", "Title", "標題", "changed since extraction"),
        ('s1 = "\\#{x} Name"\n', "s0:1:5", "#{x} Name", "#{x} 名前", "does not run"),
        ('s1 = "#\\{x} Name"\n', "s0:1:5", "#{x} Name", "#{x} 名前", "does not run"),
    ],
)
def test_a_target_that_cannot_be_written_safely_is_refused(
    source, address, text, target, reason, menu
):
    file = menu(source)
    blob, refusals = rewrite_scripts(file, {address: (text, target)})
    assert blob == file.data.blob
    [refusal] = refusals
    assert refusal.startswith(f"Menu line {address.split(':')[1]} ({address})")
    assert reason in refusal


def test_japanese_text_in_an_array_stays_untranslated_and_an_index_key_is_a_key(menu):
    texts, problems = script_texts(
        menu('menu = ["購入", "売却"]\n$imported["A_Script"] = true\n'), {}
    )
    assert problems == []
    assert [(t.text, t.status, t.key) for t in texts] == [
        ("購入", Status.UNTRANSLATED, False),
        ("売却", Status.UNTRANSLATED, False),
        ("A_Script", Status.IGNORED, True),
    ]


def test_a_target_equal_to_the_text_leaves_the_raw_form_alone(menu):
    file = menu('s1 = "HP #@hp"\r\n')
    assert rewrite_scripts(file, {"s0:1:5": ("HP #{@hp}", "HP #{@hp}")}) == (file.data.blob, [])


def test_a_translation_keeps_its_interpolation_and_the_crlf_lines_around_it(menu):
    file = menu('s1 = "HP #@hp"\r\ns2 = "MP"\r\n')
    blob, refusals = rewrite_scripts(
        file, {"s0:1:5": ("HP #{@hp}", "體力 #{@hp}"), "s0:2:5": ("MP", "魔力")}
    )
    assert refusals == []
    assert load_scripts(blob).entries[0].source == 's1 = "體力 #{@hp}"\r\ns2 = "魔力"\r\n'


def test_a_rewrite_that_does_not_read_back_as_the_same_literals_is_refused(monkeypatch, menu):
    from bokucho.engines import rpgmaker_scripts

    monkeypatch.setattr(rpgmaker_scripts, "encode", lambda text, quote: text)
    file = menu('s1 = "Name"\n')
    blob, [refusal] = rewrite_scripts(file, {"s0:1:5": ("Name", '名"前')})
    assert blob == file.data.blob
    assert refusal.startswith("Menu line 1 (s0:1:5)") and "reads back" in refusal


class _Recorder:
    def __init__(self, events):
        self._events = events

    def publish(self, event):
        self._events.append(event)


FAMILY = "Bokucho UmePlus Gothic"


@pytest.mark.parametrize("build", ["xp_scripts", "ace_scripts"])
def test_the_font_loader_runs_first_and_nothing_else_changes(build, request):
    blob = request.getfixturevalue(build)(
        [("Vocab", "module Vocab\nend\n"), ("Main", "rgss_main { }\n")]
    )
    first, second = load_scripts(with_font_loader(blob, FAMILY)).entries
    assert first.name == "Vocab"
    assert first.source.startswith("# Bokucho") and first.source.endswith("module Vocab\nend\n")
    assert f"'{FAMILY}'" in first.source
    assert second.source == "rgss_main { }\n"


def test_the_setter_is_wrapped_once_so_f12_does_not_recurse(xp_scripts):
    """F12 runs every script again in the same Ruby; a second alias would make the wrapper
    call itself on the next assignment."""
    blob = xp_scripts([("Main", "rgss_main { }\n")])
    [entry] = load_scripts(with_font_loader(blob, FAMILY)).entries
    guard = entry.source.index("unless method_defined?(:bokucho_default_name=)")
    assert guard < entry.source.index("alias_method") < entry.source.index("def default_name=")


def test_a_magic_comment_stays_on_the_first_line(xp_scripts):
    blob = xp_scripts([("Vocab", "# -*- coding: utf-8 -*-\nmodule Vocab\nend\n")])
    [entry] = load_scripts(with_font_loader(blob, FAMILY)).entries
    assert entry.source.startswith("# -*- coding: utf-8 -*-\n# Bokucho")


@pytest.mark.parametrize("family", ["Bokucho X' + y", "Other"])
def test_a_name_that_is_not_plain_is_refused(xp_scripts, family):
    with pytest.raises(ValueError):
        with_font_loader(xp_scripts([("A", "a = 1\n")]), family)


def test_a_first_script_that_cannot_be_read_is_refused():
    blob = dump_marshal([[0, b"Broken", b"not zlib"], [1, b"Main", zlib.compress(b"p 1\n")]])
    with pytest.raises(ValueError):
        with_font_loader(blob, FAMILY)
