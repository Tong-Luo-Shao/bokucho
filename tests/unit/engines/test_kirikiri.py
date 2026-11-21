"""KiriKiri games: which archives the game loads, and their .ks scenario text."""

from __future__ import annotations

import re

import pytest

from bokucho.application.errors import InvalidProject
from bokucho.engines import xp3
from bokucho.engines.kag import encode_script
from bokucho.engines.kirikiri import (
    KiriKiriAdapter,
    SlotRefused,
    UnsupportedCategory,
    game_layout,
    patch_slot,
)
from bokucho.infrastructure.factories import make_engine_adapter


def test_extract_reads_lines_and_names(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"scenario/first.ks": "[name fn=春香]\nおはよう\n"})
    units = KiriKiriAdapter(name_attributes=["name.fn"]).extract(game)
    assert [(u.id, u.kind, u.engine_ref["file"]) for u in units] == [
        ("first.ks::0", "dialogue", "first.ks"),
        ("name:春香", "name", "speakers"),
    ]
    assert units[0].context.speaker == "春香"


def test_a_name_used_twice_is_one_unit(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"b.ks": "[name fn=春香]\nい\n", "a.ks": "[name fn=春香]\nあ\n[name fn=秋子]\nう\n"},
    )
    units = KiriKiriAdapter(name_attributes=["name.fn"]).extract(game)
    assert [u.id for u in units] == ["a.ks::0", "a.ks::1", "b.ks::0", "name:春香", "name:秋子"]


def test_the_last_loaded_copy_is_read_and_unnamed_archives_are_ignored(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"scenario/first.ks": "古い\n"},
        extra={
            "sys/scenario.xp3": {"first.ks": encode_script("中\n")},
            "patch.xp3": {"first.ks": encode_script("新しい\n")},
            "other.xp3": {"first.ks": encode_script("無視\n")},
        },
    )
    assert game_layout(game).archives == ("data.xp3", "sys/scenario.xp3", "patch.xp3")
    assert [u.source for u in KiriKiriAdapter().extract(game)] == ["新しい"]


def test_the_patch_loop_archives_are_loaded_in_order(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g",
        {"a.ks": "古い\n"},
        extra={
            "patch2.xp3": {"a.ks": encode_script("二\n")},
            "patch3.xp3": {"a.ks": encode_script("三\n")},
            "patch5.xp3": {"a.ks": encode_script("五\n")},
        },
    )
    assert game_layout(game).archives == ("data.xp3", "patch2.xp3", "patch3.xp3")
    assert [u.source for u in KiriKiriAdapter().extract(game)] == ["三"]


@pytest.mark.parametrize(
    "present,slot",
    [
        ((), "patch.xp3"),
        (("patch.xp3",), "patch2.xp3"),
        (("Patch.xp3", "PATCH2.XP3"), "patch3.xp3"),
        (("patch2.xp3",), "patch3.xp3"),
    ],
)
def test_the_patch_takes_the_slot_after_the_last_loaded_archive(tmp_path, present, slot):
    for name in present:
        (tmp_path / name).write_bytes(b"")
    assert patch_slot(tmp_path, "loop") == slot


def test_a_patch_archive_after_a_gap_refuses(tmp_path):
    (tmp_path / "patch.xp3").write_bytes(b"")
    (tmp_path / "patch3.xp3").write_bytes(b"")
    with pytest.raises(SlotRefused, match="patch3.xp3"):
        patch_slot(tmp_path, "loop")


def test_a_single_patch_game_refuses_when_one_is_there(tmp_path):
    (tmp_path / "patch.xp3").write_bytes(b"")
    with pytest.raises(SlotRefused, match="patch.xp3"):
        patch_slot(tmp_path, "single")


@pytest.mark.parametrize("slots", ["loop", "single", "none"])
def test_the_startup_script_gives_the_slot_form(tmp_path, kirikiri_game, slots):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"}, slots=slots)
    assert game_layout(game).slots == {"none": ""}.get(slots, slots)


def test_single_quotes_in_the_startup_script_are_read(tmp_path):
    xp3.write(tmp_path / "data.xp3", {"startup.tjs": b"useArchiveIfExists('patch.xp3');\n"})
    assert game_layout(tmp_path).slots == "single"


def test_inject_writes_only_changed_scripts_as_utf16(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n", "b.ks": "い\n"})
    units = KiriKiriAdapter().extract(game)
    units[0].target = "甲"
    units[1].target = "  "
    KiriKiriAdapter().inject(units, game, tmp_path / "out")
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["a.ks"]
    assert (tmp_path / "out" / "a.ks").read_bytes() == encode_script("甲\n")


def test_a_name_translation_reaches_every_script(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "[name fn=春香]\nあ\n", "b.ks": "[name fn=春香]\nい\n"}
    )
    adapter = KiriKiriAdapter(name_attributes=["name.fn"])
    units = adapter.extract(game)
    units[-1].target = "春香譯"
    adapter.inject(units, game, tmp_path / "out")
    for name, line in (("a.ks", "あ"), ("b.ks", "い")):
        written = (tmp_path / "out" / name).read_bytes()
        assert written == encode_script(f'[name fn="春香譯"]\n{line}\n')


def test_a_cp932_script_comes_back_as_utf16(tmp_path, kirikiri_startup):
    xp3.write(
        tmp_path / "data.xp3",
        {"system/Initialize.tjs": kirikiri_startup(), "a.ks": "あ\n".encode("cp932")},
    )
    units = KiriKiriAdapter().extract(tmp_path)
    units[0].target = "甲"
    KiriKiriAdapter().inject(units, tmp_path, tmp_path / "out")
    assert (tmp_path / "out" / "a.ks").read_bytes() == encode_script("甲\n")


def test_one_basename_in_two_folders_is_refused(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"x/a.ks": "あ\n", "y/a.ks": "い\n"})
    with pytest.raises(InvalidProject, match="x/a.ks.*y/a.ks"):
        KiriKiriAdapter().extract(game)


# Never inject these before the guard exists: the name would be written outside out/.
@pytest.mark.parametrize("name", ["x/D:evil.ks", "x/con.ks"])
def test_a_script_name_that_is_not_a_plain_file_name_is_refused(tmp_path, kirikiri_game, name):
    game = kirikiri_game(tmp_path / "g", {name: "あ\n"})
    with pytest.raises(InvalidProject, match=re.escape(name)):
        KiriKiriAdapter().extract(game)


@pytest.mark.parametrize(
    "category,files",
    [
        (2, {"scenario/a.scn": b"PSB\0"}),
        (3, {"a.ks": b"\xfe\xfe\x01x"}),
        (3, {"a.ks": b"\x00\x14\x15x"}),
        (3, {"a.ks": encode_script("あ\n"), "image/bg.png": b"\x13\x37 not a picture"}),
    ],
)
def test_other_categories_are_named(tmp_path, kirikiri_startup, category, files):
    xp3.write(tmp_path / "data.xp3", {"system/Initialize.tjs": kirikiri_startup(), **files})
    with pytest.raises(UnsupportedCategory, match=f"category {category}") as caught:
        KiriKiriAdapter().extract(tmp_path)
    assert caught.value.category == category


def test_a_scrambled_startup_script_is_category_3(tmp_path):
    xp3.write(
        tmp_path / "data.xp3",
        {"system/Initialize.tjs": b"\xfe\xfe\x01x", "a.ks": encode_script("あ\n")},
    )
    with pytest.raises(UnsupportedCategory) as caught:
        KiriKiriAdapter().extract(tmp_path)
    assert caught.value.category == 3


def test_a_publisher_index_is_category_4(tmp_path, xp3_bytes):
    game = tmp_path / "g"
    game.mkdir()
    (game / "data.xp3").write_bytes(
        xp3_bytes([("a.ks", [(True, encode_script("あ\n"))])], extra=[(b"Hxv4", b"\0" * 8)])
    )
    with pytest.raises(UnsupportedCategory) as caught:
        KiriKiriAdapter().extract(game)
    assert caught.value.category == 4


def test_a_game_without_scenarios_is_refused(tmp_path, kirikiri_startup):
    xp3.write(tmp_path / "data.xp3", {"system/Initialize.tjs": kirikiri_startup()})
    with pytest.raises(InvalidProject, match=r"\.ks"):
        KiriKiriAdapter().extract(tmp_path)


def test_a_folder_without_data_xp3_is_refused(tmp_path):
    with pytest.raises(InvalidProject, match="data.xp3"):
        game_layout(tmp_path)


def test_a_damaged_archive_is_named(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"})
    (game / "patch.xp3").write_bytes(b"not an archive")
    with pytest.raises(InvalidProject, match="patch.xp3"):
        KiriKiriAdapter().extract(game)


def test_archive_names_match_in_any_case(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"Patch.xp3": {"b.ks": encode_script("い\n")}}
    )
    (game / "data.xp3").rename(game / "DATA.XP3")
    assert game_layout(game).archives == ("DATA.XP3", "Patch.xp3")


def test_the_given_archives_replace_the_layout(tmp_path, kirikiri_game):
    game = kirikiri_game(
        tmp_path / "g", {"a.ks": "あ\n"}, extra={"patch.xp3": {"a.ks": encode_script("い\n")}}
    )
    assert [u.source for u in KiriKiriAdapter(archives=["data.xp3"]).extract(game)] == ["あ"]
    with pytest.raises(InvalidProject, match="gone.xp3"):
        KiriKiriAdapter(archives=["data.xp3", "gone.xp3"]).extract(game)


def test_an_executable_path_means_its_folder(tmp_path, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "あ\n"})
    (game / "game.exe").write_bytes(b"MZ")
    assert KiriKiriAdapter.game_root(game / "game.exe") == game
    assert KiriKiriAdapter.game_root(game) == game


def test_the_config_reaches_the_adapter(tmp_path, new_project, kirikiri_game):
    game = kirikiri_game(tmp_path / "g", {"a.ks": "[en]A[endif]\n[name fn=春香]B\n*x_en\nC\n"})
    project = new_project(engine="kirikiri")
    project.config.engine.kag_skip_branches = ["en"]
    project.config.engine.kag_name_attributes = ["name.fn"]
    project.config.engine.kag_skip_labels = ["*_en"]
    units = make_engine_adapter(project).extract(game)
    assert [(u.id, u.source) for u in units] == [("a.ks::0", "B"), ("name:春香", "春香")]
