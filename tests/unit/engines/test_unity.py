"""Tests for the Unity engine adapter."""

import struct
from types import SimpleNamespace

import pytest
from PIL import Image

from bokucho.application.contracts import ProgressLevel
from bokucho.application.errors import InvalidProject
from bokucho.engines.unity import (
    CONFIG_FILE,
    TRANSLATION_FILE,
    UnityAdapter,
    UnityLayout,
    encode_entry,
    text_asset_body,
    text_asset_strings,
    translated_texture_name,
    translation_file,
    unit_id,
    user_strings,
    wanted,
    xunity_hash,
    xunity_language,
)
from bokucho.engines.unity_il2cpp import METADATA
from bokucho.model import TextUnit


def assembly(literals: list[str]) -> bytes:
    """A minimal PE32 image whose only metadata stream is a ``#US`` heap of ``literals``."""
    heap = b"\0"
    for text in literals:
        data = text.encode("utf-16-le") + b"\x01"
        size = len(data)
        heap += (bytes([size]) if size < 0x80 else bytes([0x80 | size >> 8, size & 0xFF])) + data
    heap += b"\0" * (-len(heap) % 4)
    version = b"v4.0.30319\0\0"
    root = struct.pack("<4sHHII", b"BSJB", 1, 1, 0, len(version)) + version
    root += struct.pack("<HH", 0, 1) + struct.pack("<II", len(root) + 16, len(heap)) + b"#US\0"
    metadata = root + heap
    cli = struct.pack("<IHHII", 72, 2, 5, 0x2000 + 72, len(metadata)).ljust(72, b"\0")
    body = cli + metadata
    image = bytearray(0x200)
    struct.pack_into("<I", image, 0x3C, 0x40)
    image[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HH12xHH", image, 0x44, 0x14C, 1, 224, 0)
    struct.pack_into("<H", image, 0x58, 0x10B)
    struct.pack_into("<II", image, 0x58 + 96 + 14 * 8, 0x2000, 72)
    struct.pack_into("<8s4I", image, 0x58 + 224, b".text", len(body), 0x2000, len(body), 0x200)
    return bytes(image) + body


def unity_game(tmp_path, *, il2cpp=False, dlls=None):
    game = tmp_path / "game"
    data = game / "Game_Data"
    (data / "Managed").mkdir(parents=True)
    (data / "globalgamemanagers").write_bytes(b"")
    if il2cpp:
        (game / "GameAssembly.dll").write_bytes(b"")
    for name, literals in (dlls or {}).items():
        (data / "Managed" / name).write_bytes(assembly(literals))
    return game


def test_user_strings_reads_every_literal_in_order():
    long = "長" * 100  # 201 bytes: the two-byte length form
    assert user_strings(assembly(["はい", "", "Start", long])) == ["はい", "", "Start", long]


def test_user_strings_refuses_a_file_that_is_not_an_assembly():
    with pytest.raises(ValueError, match="not a .NET assembly"):
        user_strings(b"MZ" + b"\0" * 100)


@pytest.mark.parametrize(
    ("text", "source", "kept"),
    [
        ("好きな色をひとつ選んでください", "ja", True),
        ("攻撃", "ja", True),
        ("UI/Button_01", "ja", False),
        ("  ", "ja", False),
        ("\x00\x01バイナリ", "ja", False),
        ("Start", "en", False),
        ("Press any key to start", "en", True),
        ("안녕하세요", "ko", True),
        ("こんにちは", "ko", False),
    ],
)
def test_wanted_keeps_source_language_text_only(text, source, kept):
    assert wanted(text, source) is kept


def test_text_asset_body_decodes_boms_and_refuses_binary():
    assert text_asset_body("﻿こんにちは".encode()) == "こんにちは"
    assert text_asset_body(b"\xff\xfe" + "台詞".encode("utf-16-le")) == "台詞"
    as_unitypy_reads_it = b"\xff\xfe" + "台詞".encode("utf-16-le")
    assert text_asset_body(as_unitypy_reads_it.decode("utf-8", "surrogateescape")) == "台詞"
    assert text_asset_body(b"\x89PNG\x00\xff\xfe\xfd"[4:] + b"\xc3") is None


def test_text_asset_strings_reads_json_tables_and_lines():
    assert list(text_asset_strings('{"name": "Key", "lines": ["一行目", "二行目"]}')) == [
        "Key", "一行目", "二行目"
    ]
    assert list(text_asset_strings("id,ja,en\n1,\"はい、そう\",Yes\n")) == [
        "id", "ja", "en", "1", "はい、そう", "Yes"
    ]
    assert list(text_asset_strings("id\tja\n1\tはい\n")) == ["id", "ja", "1", "はい"]
    assert list(text_asset_strings("一行目\n二行目、です\n")) == ["一行目", "二行目、です"]


def test_encode_entry_escapes_what_xunity_reads_as_syntax():
    assert encode_entry("a=b\\c\nd\re") == "a\\=b\\\\c\\nd\\re"
    assert encode_entry("http://x") == "http:/\\u002Fx"


def test_translation_file_has_one_line_per_pair():
    text = translation_file({"はい=いいえ": "是=否", "改行\nあり": "換行\n有"})
    assert text.splitlines()[1:] == ["はい\\=いいえ=是\\=否", "改行\\nあり=換行\\n有"]


def test_xunity_language_uppercases_the_region():
    assert xunity_language("zh-tw") == "zh-TW"
    assert xunity_language("ja") == "ja"


def test_layout_finds_the_data_folder_and_framework_free_assemblies(tmp_path):
    game = unity_game(tmp_path, dlls={"Assembly-CSharp.dll": [], "UnityEngine.UI.dll": []})
    layout = UnityLayout(game)
    assert layout.data.name == "Game_Data"
    assert not layout.il2cpp
    assert [path.name for path in layout.assemblies()] == ["Assembly-CSharp.dll"]


def test_layout_refuses_a_folder_without_unity_data(tmp_path):
    with pytest.raises(InvalidProject, match="not a Unity game folder"):
        UnityLayout(tmp_path)


def il2cpp_metadata(literals: list[str]) -> bytes:
    """A version 29 ``global-metadata.dat`` holding only a string literal table."""
    encoded = [text.encode("utf-8") for text in literals]
    starts = [sum(len(item) for item in encoded[:index]) for index in range(len(encoded))]
    table = b"".join(
        struct.pack("<II", len(item), start) for item, start in zip(encoded, starts, strict=True)
    )
    data = b"".join(encoded)
    header = struct.pack("<Ii", 0xFAB11BAF, 29)
    header += struct.pack("<ii", 64, len(table)) + struct.pack("<ii", 64 + len(table), len(data))
    return header.ljust(64, b"\0") + table + data


def il2cpp_game(tmp_path, literals=None):
    game = unity_game(tmp_path, il2cpp=True)
    (game / "Game_Data" / "Managed").rmdir()
    metadata = game / "Game_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat"
    metadata.parent.mkdir(parents=True)
    metadata.write_bytes(il2cpp_metadata(literals or []))
    return game


def test_extract_reads_il2cpp_literals_from_the_metadata(tmp_path, monkeypatch):
    game = il2cpp_game(tmp_path, ["セーブしました", "Player", "はい"])
    monkeypatch.setattr(UnityAdapter, "_asset_strings", lambda self, layout, token: iter(()))
    units = UnityAdapter(source_lang="ja").extract(game)
    assert [(unit.source, unit.engine_ref) for unit in units] == [
        ("セーブしました", {"file": METADATA, "kind": "code"}),
        ("はい", {"file": METADATA, "kind": "code"}),
    ]


def test_extract_warns_and_keeps_the_assets_when_the_metadata_is_unreadable(
    tmp_path, monkeypatch
):
    from bokucho.application.contracts import ProgressLevel

    game = il2cpp_game(tmp_path)
    (game / "Game_Data" / "il2cpp_data" / "Metadata" / "global-metadata.dat").write_bytes(
        b"\0" * 64
    )

    def assets(self, layout, token):
        yield "level0", "mono_behaviour", "Dialog", "はい"

    monkeypatch.setattr(UnityAdapter, "_asset_strings", assets)
    events = []
    sink = type("Sink", (), {"publish": lambda self, event: events.append(event)})()
    units = UnityAdapter(source_lang="ja").extract(game, sink)
    assert [unit.source for unit in units] == ["はい"]
    warnings = [event for event in events if event.level is ProgressLevel.WARNING]
    assert len(warnings) == 1 and "encrypted" in warnings[0].detail


def test_layout_names_the_backend_and_finds_text_mesh_pro_in_an_il2cpp_build(tmp_path):
    game = il2cpp_game(tmp_path)
    layout = UnityLayout(game)
    assert layout.backend == "il2cpp"
    assert layout.metadata().is_file()
    assert not layout.uses_text_mesh_pro()
    (game / "Game_Data" / "ScriptingAssemblies.json").write_text(
        '{"names":["Assembly-CSharp.dll","Unity.TextMeshPro.dll"],"types":[16,16]}',
        encoding="utf-8",
    )
    assert layout.uses_text_mesh_pro()


class _Generator:
    loaded: list = []

    def __init__(self, version):
        self.version = version

    def load_il2cpp(self, assembly, metadata):
        self.loaded.append(("il2cpp", assembly, metadata[:4]))

    def load_dll_folder(self, folder):
        self.loaded.append(("mono", folder))


def _fake_type_tree_module(monkeypatch, generator):
    import sys
    import types

    for name in ("UnityPy", "UnityPy.helpers"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType("UnityPy.helpers.TypeTreeGenerator")
    module.TypeTreeGenerator = generator
    monkeypatch.setitem(sys.modules, "UnityPy.helpers.TypeTreeGenerator", module)


def _env():
    from types import SimpleNamespace

    file = SimpleNamespace(unity_version="2022.3.10f1")
    return SimpleNamespace(objects=[SimpleNamespace(assets_file=file)], typetree_generator=None)


def test_type_trees_load_from_the_game_code_of_either_backend(tmp_path, monkeypatch):
    from bokucho.engines.unity import load_type_trees

    _fake_type_tree_module(monkeypatch, _Generator)
    _Generator.loaded = []
    game = il2cpp_game(tmp_path / "a")
    (game / "GameAssembly.dll").write_bytes(b"MZ code")
    env = _env()
    load_type_trees(env, UnityLayout(game))
    assert _Generator.loaded == [("il2cpp", b"MZ code", b"\xaf\x1b\xb1\xfa")]
    assert isinstance(env.typetree_generator, _Generator)

    mono = unity_game(tmp_path / "b")
    env = _env()
    load_type_trees(env, UnityLayout(mono))
    assert _Generator.loaded[-1] == ("mono", str(mono / "Game_Data" / "Managed"))


def test_a_failing_type_tree_generator_is_left_off(tmp_path, monkeypatch):
    from bokucho.engines.unity import load_type_trees

    class Broken(_Generator):
        def load_il2cpp(self, assembly, metadata):
            raise RuntimeError("unsupported metadata")

    _fake_type_tree_module(monkeypatch, Broken)
    game = il2cpp_game(tmp_path)
    env = _env()
    load_type_trees(env, UnityLayout(game))
    assert env.typetree_generator is None


def test_extract_reads_assets_and_code_once_per_text(tmp_path, monkeypatch):
    game = unity_game(
        tmp_path,
        dlls={"Assembly-CSharp.dll": ["セーブしました", "Player", "はい"]},
    )

    def assets(self, layout, token):
        yield "level0", "mono_behaviour", "Dialog", "はい"
        yield "level0", "unreadable", "", ""
        yield "resources.assets", "text_asset", "Story", "物語が始まる"

    monkeypatch.setattr(UnityAdapter, "_asset_strings", assets)
    units = UnityAdapter(source_lang="ja").extract(game)
    assert [(unit.source, unit.engine_ref) for unit in units] == [
        ("はい", {"file": "level0", "kind": "mono_behaviour", "asset": "Dialog"}),
        ("物語が始まる", {"file": "resources.assets", "kind": "text_asset", "asset": "Story"}),
        ("セーブしました", {"file": "Managed/Assembly-CSharp.dll", "kind": "code"}),
    ]
    assert units[0].id == unit_id("はい")


def test_inject_writes_the_translation_and_a_config_without_a_service(tmp_path):
    units = [
        TextUnit(id=unit_id("はい"), source="はい", target="是"),
        TextUnit(id=unit_id("いいえ"), source="いいえ"),
    ]
    UnityAdapter(source_lang="ja", target_lang="zh-tw").inject(units, tmp_path, tmp_path / "out")
    text = tmp_path / "out" / "BepInEx" / "Translation" / "zh-TW" / "Text" / TRANSLATION_FILE
    assert text.read_text(encoding="utf-8").splitlines()[1:] == ["はい=是"]
    config = (tmp_path / "out" / CONFIG_FILE).read_text(encoding="utf-8")
    assert "Endpoint=\n" in config
    assert "Language=zh-TW" in config and "FromLanguage=ja" in config
    assert "[Texture]" not in config


def test_inject_turns_textures_on_and_keeps_picture_text_out_of_the_table(tmp_path):
    units = [
        TextUnit(id=unit_id("はい"), source="はい", target="是"),
        TextUnit(id="img:Title [768E0C1C69].png", source="タイトル", target="標題", kind="image"),
    ]
    UnityAdapter(textures=True).inject(units, tmp_path, tmp_path / "out")
    text = tmp_path / "out" / "BepInEx" / "Translation" / "zh-TW" / "Text" / TRANSLATION_FILE
    assert text.read_text(encoding="utf-8").splitlines()[1:] == ["はい=是"]
    config = (tmp_path / "out" / CONFIG_FILE).read_text(encoding="utf-8")
    assert (
        "[Texture]\nEnableTextureTranslation=True\nTextureHashGenerationStrategy=FromImageName\n"
    ) in config


def fake_textures(*textures):
    """UnityPy-like Texture2D objects from (name, picture, or the error reading it)."""

    def texture(name, picture):
        def read():
            if isinstance(picture, Exception):
                raise picture
            return SimpleNamespace(m_Name=name, image=picture)

        return SimpleNamespace(read=read, path_id=7)

    return [texture(name, picture) for name, picture in textures]


def picture(color):
    return Image.new("RGBA", (4, 2), color)


def test_unpack_images_names_each_texture_by_the_key_xunity_finds_it_by(tmp_path, monkeypatch):
    game = unity_game(tmp_path)
    textures = fake_textures(
        ("标题中文版", picture("red")),
        ("Menu/Start", picture("blue")),
        ("Title", picture("green")),
        ("Title", picture("green")),  # one picture kept in two asset files
        ("Logo", picture("red")),
        ("Logo", picture("blue")),  # two pictures XUnity cannot tell apart
        ("", picture("red")),  # XUnity finds no unnamed texture
        ("Font Texture", PermissionError("no stream data")),
    )
    monkeypatch.setattr(UnityAdapter, "_textures", staticmethod(lambda layout: textures))
    events = []
    destination = tmp_path / "unpacked" / "Game_Data"
    UnityAdapter.unpack_images(game, destination, SimpleNamespace(publish=events.append))
    assert sorted(path.name for path in destination.iterdir()) == [
        "Menu_Start [5EF56FC1BE].png",
        "Title [768E0C1C69].png",
        "标题中文版 [8F8F9583A0].png",
    ]
    with Image.open(destination / "标题中文版 [8F8F9583A0].png") as unpacked:
        assert unpacked.getpixel((0, 0)) == (255, 0, 0, 255)
    warned = [event.detail for event in events if event.level is ProgressLevel.WARNING]
    assert len(warned) == 2
    assert any("Logo" in detail for detail in warned)
    assert any("no stream data" in detail for detail in warned)


def test_unpack_images_unpacks_again_once_the_game_changes(tmp_path, monkeypatch):
    game = unity_game(tmp_path)
    textures = fake_textures(("Title", picture("red")))
    monkeypatch.setattr(UnityAdapter, "_textures", staticmethod(lambda layout: textures))
    destination = tmp_path / "unpacked" / "Game_Data"
    UnityAdapter.unpack_images(game, destination)
    textures[:] = fake_textures(("Logo", picture("red")))
    UnityAdapter.unpack_images(game, destination)
    assert [path.name for path in destination.iterdir()] == ["Title [768E0C1C69].png"]
    (game / "Game_Data" / "sharedassets0.assets").write_bytes(b"updated")
    UnityAdapter.unpack_images(game, destination)
    assert [path.name for path in destination.iterdir()] == ["Logo [83FCE83274].png"]


def test_a_translated_texture_carries_the_original_hash_and_an_unchanged_one_is_left_out():
    name = translated_texture_name("Title [768E0C1C69].png", b"original", b"translated")
    assert name == f"Title [768E0C1C69-{xunity_hash(b'original')}].png"
    assert translated_texture_name("Title [768E0C1C69].png", b"same", b"same") is None


def test_architecture_reads_the_pe_machine(tmp_path):
    from bokucho.engines.unity import architecture

    for machine, expected in ((0x8664, "x64"), (0x14C, "x86")):
        image = bytearray(0x200)
        struct.pack_into("<I", image, 0x3C, 0x40)
        image[0x40:0x44] = b"PE\0\0"
        struct.pack_into("<H", image, 0x44, machine)
        (tmp_path / "UnityPlayer.dll").write_bytes(bytes(image))
        assert architecture(tmp_path / "UnityPlayer.dll") == expected
    (tmp_path / "UnityPlayer.dll").write_bytes(b"MZ")
    with pytest.raises(InvalidProject, match="not a Windows"):
        architecture(tmp_path / "UnityPlayer.dll")


def test_qa_protects_format_items_and_rich_text():
    from bokucho.qa import translation_tokens

    tokens = translation_tokens("<color=red>{0}</color>を手に入れた", "unity")
    assert {(token["token"], token["kind"]) for token in tokens} == {
        ("{0}", "variable"),
        ("<color=red>", "formatting"),
        ("</color>", "formatting"),
    }


def test_detect_offers_unity_as_supported(tmp_path):
    from bokucho.detect import detect

    game = unity_game(tmp_path)
    (game / "UnityPlayer.dll").write_bytes(b"MZ")
    found = {item.engine: item for item in detect(game)}
    assert found["unity"].supported


@pytest.mark.parametrize(
    ("source", "target", "rule"),
    [
        (
            "荷物が{where}へ届いた。",
            "包裹已送到{where}。",
            'sr:"^荷物が(.+?)へ届いた。$"=包裹已送到${1}。',
        ),
        (
            "{giver}が{taker}に花を渡した。",
            "{taker}從{giver}手中收到了花。",
            'sr:"^(.+?)が(.+?)に花を渡した。$"=${2}從${1}手中收到了花。',
        ),
        (
            "残り{0:N0}個(最大)",
            "剩餘 {0:N0} 個（最多）",
            'sr:"^残り(.+?)個[(]最大[)]$"=剩餘 ${1} 個（最多）',
        ),
        ("{name}と{name}だ", "{name}和{name}", 'sr:"^(.+?)と(.+?)だ$"=${1}和${1}'),
        ("所持金{0}$", "持有{0}$", 'sr:"^所持金(.+?)[$]$"=持有${1}$$'),
        (
            "会場は{room}です。\n行ってみよう",
            "會場在{room}。\n去看看吧",
            r'sr:"^会場は(.+?)です。\n行ってみよう$"=會場在${1}。\n去看看吧',
        ),
    ],
)
def test_template_rule_turns_placeholders_into_captures(source, target, rule):
    from bokucho.engines.unity import template_rule

    assert template_rule(source, target) == rule


@pytest.mark.parametrize("source", ["はい", "{0}", "{0}個", 'a"{0}"です', "^{0}です"])
def test_template_rule_leaves_plain_text_and_risky_templates_alone(source):
    from bokucho.engines.unity import template_rule

    assert template_rule(source, "x") is None


def test_translation_file_writes_a_rule_for_a_template():
    assert translation_file({"{item}が必要": "需要 {item}", "はい": "是"}).splitlines()[1:] == [
        'sr:"^(.+?)が必要$"=需要 ${1}',
        "はい=是",
    ]


def test_text_asset_strings_reads_a_table_with_bare_carriage_returns():
    assert list(text_asset_strings("id,ja,en\r1,はい\rです,Yes\r")) == [
        "id", "ja", "en", "1", "はい", "です", "Yes"
    ]


def test_asset_group_names_the_asset_or_its_bundle_not_the_cab_file():
    from types import SimpleNamespace

    # CI installs no unity extra; the rest of this file needs no UnityPy.
    files = pytest.importorskip("UnityPy.files")
    BundleFile = files.BundleFile

    from bokucho.engines.unity import asset_group

    bundle = BundleFile.__new__(BundleFile)
    bundle.name = "credits"
    in_bundle = SimpleNamespace(name="CAB-2398830b52b39fcabf5612f69e6fcdce", parent=bundle)
    loose = SimpleNamespace(name="level3", parent=object())
    assert asset_group("text_asset", "achieve", in_bundle) == "TextAsset/achieve"
    assert asset_group("mono_behaviour", "Dialog", in_bundle) == "credits"
    assert asset_group("mono_behaviour", "Dialog", loose) == "level3"


def test_layout_reads_the_unity_version_and_finds_text_mesh_pro(tmp_path):
    game = unity_game(tmp_path, dlls={"Unity.TextMeshPro.dll": []})
    header = b"\0" * 8 + b"\0\0\0\x16" + b"\0" * 32 + b"2019.4.29f1\0" + b"\0" * 40
    (game / "Game_Data" / "globalgamemanagers").write_bytes(header)
    layout = UnityLayout(game)
    assert layout.version() == "2019.4.29f1"
    assert layout.uses_text_mesh_pro()
    (game / "Game_Data" / "Managed" / "Unity.TextMeshPro.dll").unlink()
    assert not layout.uses_text_mesh_pro()


@pytest.mark.parametrize(
    ("version", "generation"),
    [
        ("2017.4.40f1", None),
        ("2018.4.36f1", "u2018"),
        ("2019.4.29f1", "u2019"),
        ("2020.3.48f1", "u2019"),
        ("2021.3.45f1", "u2021"),
        ("2023.2.20f1", "u2022"),
        ("6000.3.10f1", "u6000"),
        (None, None),
    ],
)
def test_font_generation_follows_the_unity_year(version, generation):
    from bokucho.engines.unity import font_generation

    assert font_generation(version) == generation


def test_inject_names_the_fallback_font_in_the_config(tmp_path):
    units = [TextUnit(id=unit_id("はい"), source="はい", target="是")]
    adapter = UnityAdapter(fallback_font="BepInEx/fonts/bokucho_tc_sdf_u2019")
    adapter.inject(units, tmp_path, tmp_path / "out")
    config = (tmp_path / "out" / CONFIG_FILE).read_text(encoding="utf-8")
    assert "[Behaviour]\nFallbackFontTextMeshPro=BepInEx/fonts/bokucho_tc_sdf_u2019\n" in config


def test_text_asset_strings_reads_the_values_of_a_key_value_file():
    body = (
        "# comment\r\n"
        "m.andlist2 = {0} と {1}\r\n"
        "m.intro = 一行目\\n\\" "\r\n"
        "    二行目\r\n"
        "\r\n"
        "m.title=スライム\r\n"
    )
    assert list(text_asset_strings(body)) == ["{0} と {1}", "一行目\n二行目", "スライム"]
    assert list(text_asset_strings("ただの文章です。\na = b\n")) == ["ただの文章です。", "a = b"]


def test_wanted_drops_unicode_range_tables_found_in_real_il2cpp_metadata():
    # Shapes taken from .NET's regex tables, which IL2CPP merges into the game's literals.
    table = "$%+,0:<?A[^_`{|}~\x7f¢«¬®·¸»¼¿ÀȡȢȴɐʮʰ˯" + "ΆΈΌΎΣϏϐ" * 8 + "ぁゔァヺ"
    assert not wanted(table, "ja")
    assert not wanted("　぀", "ja")  # U+3040 is unassigned
    assert not wanted("AZaz" + "ÀÖØöøıĴľŁňŊ" * 6 + "あ", "ja")
    assert wanted("<ruby=いち>壱</ruby>", "ja")


def test_il2cpp_extraction_leaves_out_the_framework_calendar_literals(tmp_path, monkeypatch):
    game = il2cpp_game(tmp_path, ["明治", "年", "令和", "セーブしました"])
    monkeypatch.setattr(UnityAdapter, "_asset_strings", lambda self, layout, token: iter(()))
    assert [unit.source for unit in UnityAdapter(source_lang="ja").extract(game)] == [
        "セーブしました"
    ]


def test_extract_reads_the_text_files_a_game_keeps_beside_its_assets(tmp_path, monkeypatch):
    game = unity_game(tmp_path)
    languages = game / "Data" / "Text"
    (languages / "English").mkdir(parents=True)
    (languages / "English" / "Town.txt").write_text(
        "Shop\n\nHi, I'm Rin!\nWhat's your name?\n---\n", encoding="utf-8"
    )
    (languages / "English" / "Notes.txt").write_bytes(b"\xef\xbb\xbfA letter came today\n")
    (languages / "Japanese").mkdir()
    (languages / "Japanese" / "Town.txt").write_text("ショップ\n", encoding="utf-8")
    monkeypatch.setattr(UnityAdapter, "_asset_strings", lambda self, layout, token: iter(()))
    adapter = UnityAdapter(source_lang="en", text_files=("Data/Text/English/*.txt",))
    units = adapter.extract(game)
    english = "Data/Text/English/"
    assert [(unit.source, unit.engine_ref["file"]) for unit in units] == [
        ("A letter came today", english + "Notes.txt"),
        ("Shop", english + "Town.txt"),  # a one-word label stays in a text file
        ("Hi, I'm Rin!", english + "Town.txt"),
        ("What's your name?", english + "Town.txt"),
    ]
    assert {unit.engine_ref["kind"] for unit in units} == {"text_file"}


@pytest.mark.parametrize("pattern", ["../other/*.txt", "/etc/*", "C:/Games/*.txt", " "])
def test_text_files_must_stay_inside_the_game_folder(tmp_path, pattern):
    with pytest.raises(InvalidProject, match="inside the game folder"):
        UnityAdapter(text_files=(pattern,)).extract(unity_game(tmp_path))


def test_the_project_carries_its_text_files_to_the_adapter(tmp_path):
    from bokucho.application.projects import CreateProjectRequest, create_project
    from bokucho.infrastructure.factories import make_engine_adapter
    from bokucho.project import Project

    project = create_project(
        CreateProjectRequest(tmp_path / "project", "story", "en", "zh-tw", "unity")
    )
    engine = project.config.engine.model_copy(update={"text_files": ["Data/*.txt"]})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    loaded = Project.load(project.root)
    assert loaded.config.engine.text_files == ["Data/*.txt"]
    assert make_engine_adapter(loaded).text_files == ("Data/*.txt",)


def test_text_asset_strings_reads_a_translation_document_with_its_source_comments():
    # A localization document as some games ship one: a ";" comment holds the line in the
    # source language, the key under it the same line in another.
    body = (
        "; zh-CN -> en\n"
        "\n"
        "; 店员\n"
        "Name1: Clerk\n"
        "\n"
        "Name2: \n"
        "\n"
        "; 今天天气不错。\n"
        "line1: Nice weather today.\n"
    )
    assert list(text_asset_strings(body)) == [
        "店员", "Clerk", "", "今天天气不错。", "Nice weather today."
    ]


def test_layout_accepts_the_game_executable_as_well_as_its_folder(tmp_path):
    game = unity_game(tmp_path)
    (game / "Game.exe").write_bytes(b"MZ")
    layout = UnityLayout(game / "Game.exe")
    assert layout.root == game
    assert layout.data.name == "Game_Data"


def test_translation_file_also_matches_a_label_the_game_upper_cases():
    lines = translation_file(
        {"New game": "新遊戲", "Back": "返回", "BACK": "上一頁", "はい": "是", "Go {0}": "去 {0}"}
    ).splitlines()[1:]
    assert "NEW GAME=新遊戲" in lines
    assert "BACK=上一頁" in lines and "BACK=返回" not in lines  # a real source wins
    assert not any(line.startswith("はい=") and line != "はい=是" for line in lines)
    assert not any(line.startswith("GO ") for line in lines)
