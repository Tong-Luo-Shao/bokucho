"""RPG Maker XP and VX: their own data layout, written back by splicing."""

import pytest

pytest.importorskip("rubymarshal")

from bokucho.application.contracts import ProgressLevel  # noqa: E402
from bokucho.engines.rpgmaker import (  # noqa: E402
    LineCountMismatch,
    RpgMakerAdapter,
    Variant,
    command_slots,
    database_fields,
    file_slots,
    system_names,
)
from bokucho.engines.rpgmaker_marshal import dump_marshal, load_legacy, view  # noqa: E402

SUFFIX = {Variant.XP: ".rxdata", Variant.VX: ".rvdata"}


def _legacy(value):
    return load_legacy(dump_marshal(value))


def _command(ruby, code, *parameters):
    return ruby("RPG::EventCommand", code=code, indent=0, parameters=list(parameters))


def _game(tmp_path, ruby, variant, files):
    data = tmp_path / "game" / "Data"
    data.mkdir(parents=True)
    for stem, payload in {"System": ruby("RPG::System"), **files}.items():
        (data / f"{stem}{SUFFIX[variant]}").write_bytes(dump_marshal(payload))
    return tmp_path / "game"


def test_an_xp_message_starts_in_its_101(ruby):
    commands = view(_legacy([
        _command(ruby, 101, "一行目".encode()),
        _command(ruby, 401, "二行目".encode()),
        _command(ruby, 0),
    ]).root)
    [slot] = command_slots(list(commands), "ev1", Variant.XP)
    assert (slot.address, slot.text) == ("ev1:c0", "一行目\n二行目")
    slot.write("第一行\n第二行")
    assert [command["parameters"][0] for command in list(commands)[:2]] == ["第一行", "第二行"]


def test_a_vx_message_is_the_401_run_after_its_101(ruby):
    commands = view(_legacy([
        _command(ruby, 101, b"Actor1", 0, 0, 2),
        _command(ruby, 401, "一行目".encode()),
        _command(ruby, 0),
    ]).root)
    [slot] = command_slots(list(commands), "ev1", Variant.VX)
    assert (slot.address, slot.text) == ("ev1:c1", "一行目")


@pytest.mark.parametrize("variant", [Variant.XP, Variant.VX])
def test_neither_xp_nor_vx_reads_105_as_text(ruby, variant):
    assert command_slots(list(view(_legacy([_command(ruby, 105, 1)]).root)), "ev1", variant) == []


@pytest.mark.parametrize("variant", [Variant.XP, Variant.VX])
def test_a_string_that_is_not_utf8_is_never_dialogue_or_a_choice(ruby, variant):
    sjis = "こんにちは".encode("shift_jis")
    commands = view(_legacy([
        _command(ruby, 101, sjis if variant is Variant.XP else b""),
        _command(ruby, 401, sjis),
        _command(ruby, 102, [sjis, "はい".encode()], 0),
    ]).root)
    slots = command_slots(list(commands), "ev1", variant)
    assert [(slot.address, slot.text) for slot in slots] == [("ev1:c2:1", "はい")]


def test_each_generation_has_its_own_database_fields():
    assert database_fields("Skills", Variant.XP) == ("name", "description")
    assert database_fields("States", Variant.XP) == ("name",)
    assert database_fields("Actors", Variant.VX) == ("name",)
    assert database_fields("Skills", Variant.VX) == ("name", "description", "message1", "message2")
    assert database_fields("States", Variant.VX) == (
        "name", "message1", "message2", "message3", "message4",
    )
    assert system_names(Variant.XP) == ((), ("elements",), ())
    assert system_names(Variant.VX) == (("game_title",), ("elements",), ())


def test_xp_words_and_elements_are_terms_and_file_names_are_not(ruby):
    words = ruby("RPG::System::Words", gold=b"G", hp=b"HP")
    system = ruby(
        "RPG::System", words=words, elements=[b"", "炎".encode()], windowskin_name=b"001-Blue01"
    )
    slots = file_slots("System", load_legacy(dump_marshal(system)), Variant.XP)
    assert {(slot.address, slot.text) for slot in slots} == {
        ("elements:1", "炎"), ("words:gold", "G"), ("words:hp", "HP"),
    }


def test_vx_title_terms_and_elements_are_terms(ruby):
    terms = ruby("RPG::System::Terms", new_game="ニューゲーム".encode())
    system = ruby(
        "RPG::System", game_title="勇者".encode(), terms=terms, elements=[b"", "炎".encode()]
    )
    slots = file_slots("System", load_legacy(dump_marshal(system)), Variant.VX)
    assert {(slot.address, slot.text) for slot in slots} == {
        ("game_title", "勇者"), ("terms:new_game", "ニューゲーム"), ("elements:1", "炎"),
    }


def test_a_vx_note_is_never_read(ruby):
    item = ruby("RPG::Item", id=1, name="薬".encode(), description=b"", note=b"<name_2:Potion>")
    slots = file_slots("Items", load_legacy(dump_marshal([None, item])), Variant.VX)
    assert [slot.text for slot in slots] == ["薬"]


@pytest.mark.parametrize("variant", [Variant.XP, Variant.VX])
def test_a_loose_game_round_trips_and_only_its_strings_change(tmp_path, ruby, variant):
    header = (
        _command(ruby, 101, "一行目".encode())
        if variant is Variant.XP
        else _command(ruby, 101, b"", 0, 0, 2)
    )
    body = _command(ruby, 401, "二行目".encode())
    page = ruby("RPG::Event::Page", list=[header, body, _command(ruby, 0)])
    actor = ruby("RPG::Actor", id=1, name="ハロルド".encode(), character_name=b"001-Fighter01")
    game = _game(tmp_path, ruby, variant, {
        "Actors": [None, actor],
        "Map001": ruby("RPG::Map", events={1: ruby("RPG::Event", id=1, pages=[page])}),
    })
    units = RpgMakerAdapter().extract(game)
    assert units == RpgMakerAdapter().extract(game)
    translations = {
        "ハロルド": "哈羅德・長", "一行目\n二行目": "第一行\n第二行", "二行目": "第二行",
    }
    for unit in units:
        unit.target = translations[unit.source]
    RpgMakerAdapter().inject(units, game, tmp_path / "out")
    out = tmp_path / "out" / "Data"
    expected = {
        "Actors": {("ハロルド", "哈羅德・長")},
        "Map001": (
            {("一行目", "第一行"), ("二行目", "第二行")}
            if variant is Variant.XP
            else {("二行目", "第二行")}
        ),
    }
    for stem, wanted in expected.items():
        before = load_legacy((game / "Data" / f"{stem}{SUFFIX[variant]}").read_bytes())
        after = load_legacy((out / f"{stem}{SUFFIX[variant]}").read_bytes())
        pairs = zip(before.strings, after.strings, strict=True)
        changed = {(a.decode(), b.decode()) for a, b in pairs if a != b}
        assert changed == wanted
    written = load_legacy((out / f"Actors{SUFFIX[variant]}").read_bytes())
    assert [slot.text for slot in file_slots("Actors", written, variant)] == ["哈羅德・長"]


def _system_with_a_linked_element() -> bytes:
    """An XP System whose ``elements`` are ``["", "同じ", <link to that same string>]``."""
    shared = "同じ".encode()
    return (
        b"\x04\x08o:\x10RPG::System\x06:\x0e@elements[\x08"
        + b'"\x00"' + bytes([len(shared) + 5]) + shared + b"@\x08"
    )


def test_one_shared_string_translated_two_ways_refuses_the_inject(tmp_path):
    game = tmp_path / "game"
    (game / "Data").mkdir(parents=True)
    (game / "Data" / "System.rxdata").write_bytes(_system_with_a_linked_element())
    units = RpgMakerAdapter().extract(game)
    assert [unit.engine_ref["address"] for unit in units] == ["elements:1", "elements:2"]
    units[0].target, units[1].target = "甲", "乙"
    with pytest.raises(LineCountMismatch, match="甲.*乙|乙.*甲"):
        RpgMakerAdapter().inject(units, game, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").rglob("*.*"))


def test_an_unreadable_legacy_file_costs_only_itself(tmp_path, ruby):
    actor = ruby("RPG::Actor", id=1, name="ハロルド".encode())
    game = _game(tmp_path, ruby, Variant.XP, {"Actors": [None, actor]})
    (game / "Data" / "Items.rxdata").write_bytes(b"\x04\x08[\x07" + b"\xff")
    events = []
    units = RpgMakerAdapter().extract(game, _Recorder(events))
    assert [unit.source for unit in units] == ["ハロルド"]
    warned = [event.detail for event in events if event.level is ProgressLevel.WARNING]
    assert any(detail.startswith("Items.rxdata") for detail in warned)


class _Recorder:
    def __init__(self, events):
        self._events = events

    def publish(self, event):
        self._events.append(event)
