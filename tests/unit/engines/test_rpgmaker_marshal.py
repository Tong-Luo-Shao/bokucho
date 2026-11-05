"""The Ruby Marshal codec, and the view that lets the Phase 1 walk read it."""

import pytest

pytest.importorskip("rubymarshal")

from bokucho.engines.rpgmaker_marshal import (  # noqa: E402
    dump_legacy,
    dump_marshal,
    load_legacy,
    load_marshal,
    view,
)


def test_a_string_reads_as_a_python_string(ruby, text):
    record = view(ruby("RPG::Item", name=text("回復水"), id=1))
    assert record.get("name") == "回復水"
    assert isinstance(record.get("name"), str)


def test_a_missing_field_is_none_not_an_error(ruby):
    assert view(ruby("RPG::Item", id=1)).get("description") is None


def test_writing_through_the_view_reaches_the_real_object(ruby, text):
    obj = ruby("RPG::Item", name=text("回復水"))
    record = view(obj)
    record["name"] = "治療之水"
    assert str(obj.attributes["@name"]) == "治療之水"


def test_a_hash_of_events_reads_as_the_sequence_the_walk_expects(ruby):
    events = {2: ruby("RPG::Event", id=2), 1: ruby("RPG::Event", id=1)}
    listed = list(view(ruby("RPG::Map", events=events)).get("events"))
    assert [entry.get("id") for entry in listed] == [1, 2]


def test_a_list_of_strings_reads_and_writes_by_index(ruby, text):
    names = [text("火"), text("水")]
    elements = view(ruby("RPG::System", elements=names))
    seen = elements.get("elements")
    assert list(seen) == ["火", "水"]
    seen[1] = "冰"
    assert str(names[1]) == "冰"


def test_a_nested_object_is_viewed_too(ruby):
    page = ruby("RPG::Event::Page", list=[ruby("RPG::EventCommand", code=401)])
    event = view(ruby("RPG::Event", pages=[page]))
    command = event.get("pages")[0].get("list")[0]
    assert command.get("code") == 401


def test_a_userdef_payload_survives_a_round_trip(ruby):
    from rubymarshal.classes import UserDef

    table = UserDef("Table")
    table._private_data = bytes(range(20))
    blob = dump_marshal([None, ruby("RPG::Map", data=table)])
    back = load_marshal(blob)
    assert back[1].attributes["@data"]._private_data == bytes(range(20))


def test_a_plain_string_written_back_survives_the_round_trip(ruby, text):
    obj = ruby("RPG::Item", name=text("回復水"))
    view(obj)["name"] = "治療之水"
    assert str(load_marshal(dump_marshal(obj)).attributes["@name"]) == "治療之水"


def test_a_translation_does_not_shift_the_object_link_table(ruby, text):
    """A tree with a back-reference, which is what a real map has."""
    shared = ruby("RPG::MoveRoute", repeat=True)
    payload = [
        ruby("RPG::EventCommand", parameters=[text("original"), shared]),
        shared,
    ]
    view(payload)[0]["parameters"][0] = "translated"

    back = load_marshal(dump_marshal(payload))
    assert str(back[0].attributes["@parameters"][0]) == "translated"
    assert back[1].ruby_class_name == "RPG::MoveRoute"
    assert back[1] is back[0].attributes["@parameters"][1]


def test_a_legacy_string_reads_as_text(ruby):
    data = load_legacy(dump_marshal([None, ruby("RPG::Actor", id=1, name="ハロルド".encode())]))
    assert view(data.root)[1].get("name") == "ハロルド"


def test_writing_a_legacy_string_changes_only_that_string(ruby):
    blob = dump_marshal(
        [None, ruby("RPG::Actor", id=1, name="ハロルド".encode(), nickname="勇者".encode())]
    )
    data = load_legacy(blob)
    view(data.root)[1]["name"] = "哈羅德・很長的名字"
    out = dump_legacy(data)
    name = data.strings[0]
    assert out[: name.start] == blob[: name.start]
    assert out.endswith(blob[name.end :])
    back = view(load_legacy(out).root)[1]
    assert (back.get("name"), back.get("nickname")) == ("哈羅德・很長的名字", "勇者")


def test_nothing_written_gives_back_the_original_bytes(ruby):
    blob = dump_marshal([None, ruby("RPG::Actor", id=1, name="ハロルド".encode())])
    assert dump_legacy(load_legacy(blob)) == blob


def _float_and_link() -> bytes:
    """A Ruby 1.8 float with mantissa bytes, a string, and a link back to it."""
    text = "文字".encode()
    return b"\x04\x08[\x08f\x0b0.1\x00\x9a\x99" + b'"' + bytes([len(text) + 5]) + text + b"@\x07"


def test_a_float_and_a_link_survive_a_splice():
    blob = _float_and_link()
    data = load_legacy(blob)
    view(data.root)[1] = "很長的翻譯文字"
    out = dump_legacy(data)
    assert out.startswith(blob[: data.strings[0].start]) and out.endswith(b"@\x07")
    back = view(load_legacy(out).root)
    assert (back[0], back[1], back[2]) == (0.1, "很長的翻譯文字", "很長的翻譯文字")


def test_two_translations_of_one_linked_string_are_refused():
    shown = view(load_legacy(_float_and_link()).root)
    shown[1] = "甲"
    with pytest.raises(ValueError, match="甲.*乙|乙.*甲"):
        shown[2] = "乙"


def test_a_string_that_is_not_utf8_is_not_text():
    assert not isinstance(view(load_legacy(dump_marshal([b"\x82\xa0"])).root)[0], str)


def test_a_string_cut_short_is_refused():
    with pytest.raises(ValueError, match="2"):
        load_legacy(b'\x04\x08"\x0bab')


def test_a_user_defined_object_is_carried_through_and_never_read_as_text():
    color = b"u:\x0aColor" + bytes([32 + 5]) + bytes(range(32))
    text = "文字".encode()
    blob = b"\x04\x08[\x07" + color + b'"' + bytes([len(text) + 5]) + text
    data = load_legacy(blob)
    assert [bytes(string) for string in data.strings] == [text]
    view(data.root)[1] = "很長的翻譯文字"
    assert dump_legacy(data).startswith(blob[: data.strings[0].start])


def test_a_string_with_an_encoding_keeps_its_ivars():
    from rubymarshal.classes import RubyString

    data = load_legacy(dump_marshal([RubyString("abc", {"E": True})]))
    assert (bytes(data.strings[0]), data.strings[0].ivars) == (b"abc", {"E": True})
    view(data.root)[0] = "甲乙丙"
    assert dump_legacy(data).endswith("甲乙丙".encode() + b"\x06:\x06ET")


@pytest.mark.parametrize("blob", [b'\x04\x08"', b'\x04\x08"\x02\x10'])
def test_a_length_cut_short_is_refused(blob):
    with pytest.raises(ValueError):
        load_legacy(blob)


def test_a_link_after_an_encoded_string_survives_a_longer_length_prefix():
    """Ace Scripts as Ruby writes them, [[1, name, code], name]: E-tagged, then a link."""
    name = b"a" * 122
    blob = (
        b'\x04\x08[\x07[\x08i\x06I"\x7f' + name + b"\x06:\x06ET"
        + b'I"\x09code\x06;\x00T@\x07'
    )
    data = load_legacy(blob)
    assert data.root[1] is data.root[0][1] and view(data.root)[1] == name.decode()
    assert dump_legacy(data) == blob

    view(data.root)[0][1] = "甲" * 41
    out = dump_legacy(data)
    first = data.strings[0]
    assert out[first.start : first.start + 2] == b"\x01\x7b"
    assert out.endswith(blob[first.end :])
    back = load_legacy(out).root
    assert back[1] is back[0][1] and back[1].ivars == {"E": True}
    assert view(back)[1] == "甲" * 41
