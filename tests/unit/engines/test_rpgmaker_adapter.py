"""Tests for the RPG Maker MV/MZ engine adapter."""


import json

import pytest

from bokucho.application.contracts import (
    CancellationFlag,
    NullProgressSink,
    ProgressEvent,
    ProgressLevel,
)
from bokucho.application.errors import JobCancelled
from bokucho.detect import detect
from bokucho.engines.rpgmaker import (
    LineCountMismatch,
    NotRpgMaker,
    RpgMakerAdapter,
    RpgMakerWriteError,
    Variant,
    command_slots,
    database_fields,
    dumps_data,
    file_slots,
    load_data,
    sniff,
    system_names,
    write_data,
)
from bokucho.model import Status
from bokucho.patcher.core import RECORD, sha256_of


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def publish(self, event: ProgressEvent) -> None:
        self.events.append(event)


def build_mv(root):
    """A deployed MV game: data under www/, MV's own core script."""
    data = root / "www" / "data"
    data.mkdir(parents=True)
    (root / "www" / "js").mkdir()
    (root / "www" / "js" / "rpg_core.js").write_text("//", encoding="utf-8")
    (data / "System.json").write_text('{"gameTitle":"T"}', encoding="utf-8")
    return data


def build_mz(root):
    """A deployed MZ game: data at the root, MZ's own core script."""
    data = root / "data"
    data.mkdir(parents=True)
    (root / "js").mkdir()
    (root / "js" / "rmmz_core.js").write_text("//", encoding="utf-8")
    (data / "System.json").write_text('{"gameTitle":"T"}', encoding="utf-8")
    return data


def test_sniff_finds_mv_under_www(tmp_path):
    build_mv(tmp_path)
    layout = sniff(tmp_path)
    assert layout.variant is Variant.MV
    assert layout.data_dir == tmp_path / "www" / "data"
    assert layout.relative_data.as_posix() == "www/data"


def test_sniff_finds_mz_at_the_root(tmp_path):
    build_mz(tmp_path)
    layout = sniff(tmp_path)
    assert layout.variant is Variant.MZ
    assert layout.relative_data.as_posix() == "data"


def test_sniff_accepts_the_executable_the_user_picked(tmp_path):
    build_mv(tmp_path)
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    assert sniff(exe).data_dir == tmp_path / "www" / "data"


def test_sniff_refuses_a_directory_that_is_not_rpg_maker(tmp_path):
    (tmp_path / "whatever.txt").write_text("", encoding="utf-8")
    with pytest.raises(NotRpgMaker, match="System.json"):
        sniff(tmp_path)


def test_dumps_puts_top_level_array_items_one_per_line():
    assert dumps_data([None, {"id": 1}]) == '[\nnull,\n{"id":1}\n]'


def test_dumps_keeps_cjk_readable():
    assert dumps_data({"name": "ハロルド"}) == '{"name":"ハロルド"}'


def say(text):
    return {"code": 401, "parameters": [text]}


def header(*extra):
    return {"code": 101, "parameters": ["", 0, 0, 2, *extra]}


def test_separate_messages_stay_separate():
    slots = command_slots(
        [header(), say("First."), header(), say("Second.")], "ev1:pg0", Variant.MV
    )
    assert [slot.text for slot in slots] == ["First.", "Second."]
    assert slots[0].address != slots[1].address


def test_mz_carries_a_speaker_and_mv_does_not():
    mz = command_slots([header("Harold"), say("Hi.")], "ev1:pg0", Variant.MZ)
    mv = command_slots([header("Harold"), say("Hi.")], "ev1:pg0", Variant.MV)
    assert mz[0].speaker == "Harold"
    assert mv[0].speaker == ""


def test_scrolling_text_never_inherits_the_previous_speaker():
    commands = [
        header("Harold"),
        say("Hi."),
        {"code": 105, "parameters": [2, False]},
        {"code": 405, "parameters": ["Scrolled."]},
    ]
    slots = command_slots(commands, "ev1:pg0", Variant.MZ)
    assert [slot.text for slot in slots] == ["Hi.", "Scrolled."]
    assert slots[0].speaker == "Harold"
    assert slots[1].speaker == ""


def test_back_to_back_headers_each_keep_their_own_speaker():
    slots = command_slots(
        [header("Harold"), say("Hi."), header("Mira"), say("Hey.")],
        "ev1:pg0",
        Variant.MZ,
    )
    assert [slot.speaker for slot in slots] == ["Harold", "Mira"]


def test_choices_are_each_their_own_slot():
    commands = [{"code": 102, "parameters": [["Yes", "No"], 1]}]
    slots = command_slots(commands, "ev1:pg0", Variant.MV)
    assert [slot.text for slot in slots] == ["Yes", "No"]
    assert all(slot.kind == "choice" for slot in slots)
    slots[0].write("是")
    assert commands[0]["parameters"][0] == ["是", "No"]


def test_when_choice_labels_are_left_alone():
    slots = command_slots(
        [{"code": 402, "parameters": [0, "Yes"]}], "ev1:pg0", Variant.MV
    )
    assert slots == []


def test_scrolling_text_does_not_merge_with_a_message():
    slots = command_slots(
        [say("Boxed."), {"code": 405, "parameters": ["Scrolled."]}],
        "ev1:pg0",
        Variant.MV,
    )
    assert [slot.text for slot in slots] == ["Boxed.", "Scrolled."]


def test_map_events_are_walked_through_their_pages():
    payload = {
        "events": [
            None,
            {"id": 1, "pages": [{"list": [header(), say("Hello.")]}]},
        ]
    }
    slots = file_slots("Map001", payload, Variant.MV)
    assert [slot.text for slot in slots] == ["Hello."]
    assert slots[0].address.startswith("ev1:pg0:")


def test_common_events_are_walked():
    payload = [None, {"id": 1, "list": [header(), say("Common.")]}]
    slots = file_slots("CommonEvents", payload, Variant.MV)
    assert [slot.text for slot in slots] == ["Common."]


def test_troop_battle_events_are_walked():
    payload = [None, {"id": 1, "pages": [{"list": [header(), say("Battle.")]}]}]
    slots = file_slots("Troops", payload, Variant.MV)
    assert [slot.text for slot in slots] == ["Battle."]


def test_database_records_yield_their_player_facing_fields():
    payload = [None, {"id": 1, "name": "Potion", "description": "Heals.", "note": "x"}]
    slots = file_slots("Items", payload, Variant.MV)
    assert [(slot.kind, slot.text) for slot in slots] == [
        ("name", "Potion"),
        ("description", "Heals."),
    ]


def test_note_fields_are_never_touched():
    payload = [None, {"id": 1, "name": "Potion", "note": "<plugin:secret>"}]
    assert all("secret" not in slot.text for slot in file_slots("Items", payload, Variant.MV))


def test_state_messages_are_translatable():
    payload = [None, {"id": 1, "name": "Poison", "message1": " is poisoned!"}]
    slots = file_slots("States", payload, Variant.MV)
    assert " is poisoned!" in [slot.text for slot in slots]


def test_system_terms_and_names_are_translatable():
    payload = {
        "gameTitle": "My Game",
        "currencyUnit": "G",
        "elements": ["", "Fire"],
        "terms": {
            "basic": ["Level"],
            "commands": ["Fight"],
            "params": ["Attack"],
            "messages": {"actionFailure": "No effect!"},
        },
    }
    texts = [slot.text for slot in file_slots("System", payload, Variant.MV)]
    for expected in ("My Game", "G", "Fire", "Level", "Fight", "Attack", "No effect!"):
        assert expected in texts


def test_a_database_slot_writes_back_into_the_record():
    payload = [None, {"id": 1, "name": "Potion", "description": "Heals."}]
    slots = file_slots("Items", payload, Variant.MV)
    slots[0].write("藥水")
    assert payload[1]["name"] == "藥水"


def test_an_unknown_data_file_yields_nothing():
    assert file_slots("Tilesets", [None, {"id": 1, "name": "Field"}], Variant.MV) == []


def seed_game(root):
    """An MV game with one map, one item, and one system term."""
    data = build_mv(root)
    write_data(
        data / "Map001.json",
        {"events": [None, {"id": 1, "pages": [{"list": [header(), say("Hello.")]}]}]},
    )
    write_data(data / "Items.json", [None, {"id": 1, "name": "Potion", "description": "Heals."}])
    write_data(data / "System.json", {"gameTitle": "My Game", "terms": {"basic": ["Level"]}})
    return data


def seed_game_mz(root):
    """An MZ game with one map, one item, and one system term."""
    data = build_mz(root)
    write_data(
        data / "Map001.json",
        {"events": [None, {"id": 1, "pages": [{"list": [header(), say("Hello.")]}]}]},
    )
    write_data(data / "Items.json", [None, {"id": 1, "name": "Potion", "description": "Heals."}])
    write_data(data / "System.json", {"gameTitle": "My Game", "terms": {"basic": ["Level"]}})
    return data


def test_extract_finds_text_across_files(tmp_path):
    seed_game(tmp_path)
    units = RpgMakerAdapter().extract(tmp_path)
    by_id = {unit.id: unit for unit in units}
    assert by_id["Map001:ev1:pg0:c1"].source == "Hello."
    assert by_id["Items:1:name"].source == "Potion"
    assert by_id["System:gameTitle"].source == "My Game"
    assert by_id["Items:1:name"].engine_ref == {
        "file": "Items",
        "address": "1:name",
        "kind": "name",
    }
    assert all(unit.status is Status.UNTRANSLATED for unit in units)


def test_extract_ids_are_stable_across_runs(tmp_path):
    seed_game(tmp_path)
    first = [unit.id for unit in RpgMakerAdapter().extract(tmp_path)]
    second = [unit.id for unit in RpgMakerAdapter().extract(tmp_path)]
    assert first == second


def test_inject_writes_only_the_files_it_changed(tmp_path):
    seed_game(tmp_path)
    adapter = RpgMakerAdapter()
    units = adapter.extract(tmp_path)
    for unit in units:
        if unit.id == "Items:1:name":
            unit.target = "藥水"
    out = tmp_path / "out"
    adapter.inject(units, tmp_path, out)
    assert load_data(out / "www" / "data" / "Items.json")[1]["name"] == "藥水"
    assert not (out / "www" / "data" / "Map001.json").exists()


def test_inject_names_every_mismatch_and_writes_nothing_when_any_fail(tmp_path):
    data = seed_game(tmp_path)
    write_data(
        data / "Map002.json",
        {"events": [None, {"id": 1, "pages": [{"list": [header(), say("A."), say("B.")]}]}]},
    )
    adapter = RpgMakerAdapter()
    units = adapter.extract(tmp_path)
    for unit in units:
        if unit.id == "Map001:ev1:pg0:c1":
            unit.target = "line one\nline two"
        elif unit.id == "Map002:ev1:pg0:c1":
            unit.target = "only one line"
        elif unit.id == "Items:1:name":
            unit.target = "藥水"
    out = tmp_path / "out"
    with pytest.raises(LineCountMismatch) as caught:
        adapter.inject(units, tmp_path, out)
    message = str(caught.value)
    assert "Map001" in message
    assert "Map002" in message
    assert not out.exists()


def test_inject_skips_an_unreadable_file_and_still_writes_the_rest(tmp_path):
    data = seed_game(tmp_path)
    write_data(
        data / "Weapons.json", [None, {"id": 1, "name": "Sword", "description": "Sharp."}]
    )
    adapter = RpgMakerAdapter()
    units = adapter.extract(tmp_path)
    for unit in units:
        if unit.id == "Items:1:name":
            unit.target = "藥水"
        if unit.id == "Weapons:1:name":
            unit.target = "劍"
    (data / "Weapons.json").write_text("{not valid json", encoding="utf-8")
    sink = _RecordingSink()
    out = tmp_path / "out"
    adapter.inject(units, tmp_path, out, sink)
    assert load_data(out / "www" / "data" / "Items.json")[1]["name"] == "藥水"
    assert not (out / "www" / "data" / "Weapons.json").exists()
    warnings = [event for event in sink.events if event.level is ProgressLevel.WARNING]
    assert any("Weapons.json" in (event.detail or "") for event in warnings)


def test_extract_finds_text_across_files_mz(tmp_path):
    seed_game_mz(tmp_path)
    units = RpgMakerAdapter().extract(tmp_path)
    by_id = {unit.id: unit for unit in units}
    assert by_id["Map001:ev1:pg0:c1"].source == "Hello."
    assert by_id["Items:1:name"].source == "Potion"
    assert by_id["System:gameTitle"].source == "My Game"


def test_inject_writes_under_data_not_www_data_for_mz(tmp_path):
    seed_game_mz(tmp_path)
    adapter = RpgMakerAdapter()
    units = adapter.extract(tmp_path)
    for unit in units:
        if unit.id == "Items:1:name":
            unit.target = "藥水"
    out = tmp_path / "out"
    adapter.inject(units, tmp_path, out)
    assert load_data(out / "data" / "Items.json")[1]["name"] == "藥水"
    assert not (out / "www").exists()


def test_detect_calls_an_mv_game_supported(tmp_path):
    build_mv(tmp_path)
    found = detect(tmp_path)
    assert found[0].engine == "rpgmaker-mv"
    assert found[0].supported is True


def test_a_vx_ace_archive_is_supported(tmp_path):
    (tmp_path / "Game.rgss3a").write_bytes(b"RGSSAD\0\x03" + bytes(16))
    found = detect(tmp_path)[0]
    assert found.engine == "rpgmaker-vx-ace"
    assert found.supported is True
    assert "rpgmaker-vx-ace" in found.hint


def build_ace(root, *, archived=False, build_archive=None):
    """A VX Ace game, either unpacked or shipping only its archive."""
    (root / "System").mkdir(parents=True)
    (root / "System" / "RGSS301.dll").write_bytes(b"dll")
    (root / "Game.ini").write_text(
        "[Game]\nLibrary=System\\RGSS301.dll\n", encoding="utf-8"
    )
    if archived:
        build_archive(root / "Game.rgss3a", {"Data\\System.rvdata2": b"\x04\x080"})
        return None
    data = root / "Data"
    data.mkdir()
    (data / "System.rvdata2").write_bytes(b"\x04\x080")
    return data


def test_sniff_finds_an_unpacked_vx_ace_game(tmp_path):
    data = build_ace(tmp_path)
    layout = sniff(tmp_path)
    assert layout.variant is Variant.ACE
    assert layout.data_dir == data
    assert layout.archive is None


def test_sniff_finds_a_game_that_ships_only_its_archive(tmp_path, build_archive):
    build_ace(tmp_path, archived=True, build_archive=build_archive)
    layout = sniff(tmp_path)
    assert layout.variant is Variant.ACE
    assert layout.archive == tmp_path / "Game.rgss3a"


def test_mv_and_mz_carry_no_archive(tmp_path):
    build_mv(tmp_path)
    assert sniff(tmp_path).archive is None


def test_a_marshal_file_round_trips_through_the_codec(tmp_path):
    pytest.importorskip("rubymarshal")
    from bokucho.engines.rpgmaker_marshal import dump_marshal

    path = tmp_path / "Items.rvdata2"
    path.write_bytes(dump_marshal([None, "a"]))
    payload = load_data(path)
    destination = tmp_path / "out" / "Items.rvdata2"
    write_data(destination, payload)
    assert load_data(destination) == payload


def test_a_file_of_the_wrong_shape_costs_one_file(tmp_path):
    data = build_mv(tmp_path)
    write_data(data / "Items.json", [None, {"id": 1, "name": "Potion"}])
    (data / "Weapons.json").write_text('{"oops": "an object"}', encoding="utf-8")
    units = RpgMakerAdapter().extract(tmp_path)
    assert "Potion" in [unit.source for unit in units]


def test_an_actors_blurb_is_named_differently_in_each_generation():
    assert "profile" in database_fields("Actors", Variant.MV)
    assert "description" in database_fields("Actors", Variant.ACE)
    assert "profile" not in database_fields("Actors", Variant.ACE)


def test_the_other_database_tables_agree_across_generations():
    for stem in ("Items", "Weapons", "Armors", "Skills", "States", "Enemies", "Classes"):
        assert database_fields(stem, Variant.MV) == database_fields(stem, Variant.ACE)


def test_a_table_neither_generation_translates_yields_nothing():
    assert database_fields("Tilesets", Variant.MV) is None
    assert database_fields("Tilesets", Variant.ACE) is None


def test_system_keys_are_snake_case_for_vx_ace():
    scalars, name_lists, term_lists = system_names(Variant.ACE)
    assert "game_title" in scalars and "gameTitle" not in scalars
    assert "skill_types" in name_lists
    assert "etypes" in term_lists and "equipTypes" not in name_lists


def test_system_keys_stay_camel_case_for_mv():
    scalars, name_lists, term_lists = system_names(Variant.MV)
    assert "gameTitle" in scalars
    assert "equipTypes" in name_lists
    assert "etypes" not in term_lists


def test_the_ace_database_walk_finds_a_description(ruby, text):
    pytest.importorskip("rubymarshal")
    from bokucho.engines.rpgmaker_marshal import view

    actors = view([None, ruby("RPG::Actor", id=1, name=text("ハロルド"),
                              description=text("勇者"), note=text("秘密"))])
    slots = file_slots("Actors", actors, Variant.ACE)
    assert [(slot.kind, slot.text) for slot in slots] == [
        ("name", "ハロルド"),
        ("description", "勇者"),
    ]


def test_extract_unpacks_an_archive_into_the_workspace(tmp_path, build_archive, ruby, text):
    pytest.importorskip("rubymarshal")
    from bokucho.engines.rpgmaker_marshal import dump_marshal

    game = tmp_path / "game"
    game.mkdir()
    actors = dump_marshal([None, ruby("RPG::Actor", id=1, name=text("ハロルド"))])
    build_archive(
        game / "Game.rgss3a",
        {"Data\\System.rvdata2": dump_marshal(ruby("RPG::System")),
         "Data\\Actors.rvdata2": actors},
    )
    workspace = tmp_path / "unpacked"
    units = RpgMakerAdapter(workspace=workspace).extract(game)
    assert (workspace / "Game" / "Data" / "Actors.rvdata2").is_file()
    assert [unit.source for unit in units] == ["ハロルド"]


def test_an_unreadable_scripts_file_costs_only_itself(tmp_path):
    pytest.importorskip("rubymarshal")
    game = tmp_path / "game"
    data = game / "Data"
    data.mkdir(parents=True)
    (data / "System.rvdata2").write_bytes(b"\x04\x080")
    (data / "Scripts.rvdata2").write_bytes(b"not marshal at all")
    sink = _RecordingSink()
    assert RpgMakerAdapter().extract(game, sink) == []
    warned = [event.detail for event in sink.events if event.level is ProgressLevel.WARNING]
    assert any(detail.startswith("Scripts.rvdata2") for detail in warned)


def test_unpack_refuses_an_archive_entry_that_would_escape_the_workspace(
    tmp_path, build_archive
):
    game = tmp_path / "game"
    game.mkdir()
    build_archive(
        game / "Game.rgss3a",
        {"Data\\System.rvdata2": b"good", "..\\evil.rvdata2": b"x"},
    )
    workspace = tmp_path / "unpacked"
    with pytest.raises(ValueError, match="evil"):
        RpgMakerAdapter(workspace=workspace).extract(game)
    assert not (workspace / "evil.rvdata2").exists()
    assert not (workspace / "Game").exists()
    assert not (workspace / "Game.partial").exists()


def test_unpack_is_not_repeated_once_the_workspace_already_has_it(
    tmp_path, build_archive
):
    game = tmp_path / "game"
    game.mkdir()
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"original"})
    workspace = tmp_path / "unpacked"
    adapter = RpgMakerAdapter(workspace=workspace)
    layout = adapter._unpack(sniff(game), NullProgressSink())

    unpacked_file = layout.data_dir / "System.rvdata2"
    unpacked_file.write_bytes(b"edited-by-user")

    adapter._unpack(sniff(game), NullProgressSink())

    assert unpacked_file.read_bytes() == b"edited-by-user"


def test_an_updated_archive_is_unpacked_again(tmp_path, build_archive):
    game = tmp_path / "game"
    game.mkdir()
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"version-1"})
    adapter = RpgMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._unpack(sniff(game), NullProgressSink())
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"version-2"})

    layout = adapter._unpack(sniff(game), NullProgressSink())

    assert (layout.data_dir / "System.rvdata2").read_bytes() == b"version-2"


def test_a_game_the_patch_was_applied_to_is_read_from_its_original(tmp_path, build_archive):
    game = tmp_path / "game"
    game.mkdir()
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"original"})
    adapter = RpgMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._unpack(sniff(game), NullProgressSink())
    (game / "Game.rgss3a").replace(game / "Game.rgss3a.bak")
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"translated"})
    compiled = {"Game.rgss3a": sha256_of(game / "Game.rgss3a")}
    (game / RECORD).write_text(json.dumps({"added": [], "compiled": compiled}), encoding="utf-8")

    layout = adapter._unpack(sniff(game), NullProgressSink())

    assert (layout.data_dir / "System.rvdata2").read_bytes() == b"original"


def test_a_game_this_project_patched_and_then_updated_is_unpacked_again(
    tmp_path, build_archive
):
    game = tmp_path / "game"
    game.mkdir()
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"version-1"})
    adapter = RpgMakerAdapter(workspace=tmp_path / "unpacked")
    adapter._unpack(sniff(game), NullProgressSink())
    (game / "Game.rgss3a").replace(game / "Game.rgss3a.bak")
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"translated"})
    compiled = {"Game.rgss3a": sha256_of(game / "Game.rgss3a")}
    (game / RECORD).write_text(json.dumps({"added": [], "compiled": compiled}), encoding="utf-8")
    build_archive(game / "Game.rgss3a", {"Data\\System.rvdata2": b"version-2"})

    layout = adapter._unpack(sniff(game), NullProgressSink())

    assert (layout.data_dir / "System.rvdata2").read_bytes() == b"version-2"


def test_unpack_honors_cancellation_and_leaves_no_cache(tmp_path, build_archive):
    game = tmp_path / "game"
    game.mkdir()
    build_archive(
        game / "Game.rgss3a", {f"Data\\File{i}.rvdata2": b"x" for i in range(5)}
    )
    workspace = tmp_path / "unpacked"
    token = CancellationFlag()
    token.cancel()

    with pytest.raises(JobCancelled):
        RpgMakerAdapter(workspace=workspace).extract(game, token=token)

    assert not (workspace / "Game").exists()
    assert not (workspace / "Game.partial").exists()


def test_unpack_lands_where_the_project_resolves_it(tmp_path, build_archive):
    """The adapter's unpack destination and resolve_game_root must agree."""
    from bokucho.application.projects import (
        CreateProjectRequest,
        create_project,
        resolve_game_root,
    )
    from bokucho.infrastructure.factories import make_engine_adapter
    from bokucho.project import Project

    game = tmp_path / "game-root"
    game.mkdir()
    archive = game / "Game.rgss3a"
    build_archive(archive, {"Data\\System.rvdata2": b"\x04\x080"})
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "agree", "ja", "ja", "rpgmaker")
    )
    engine = project.config.engine.model_copy(
        update={"game_path": archive.resolve().as_posix()}
    )
    project.save_config(project.config.model_copy(update={"engine": engine}))
    project = Project.load(project.root)

    unpacked = make_engine_adapter(project)._unpack(sniff(archive), NullProgressSink())

    assert resolve_game_root(project) == unpacked.root


def test_the_launcher_beside_a_loose_game_is_not_a_packed_archive(tmp_path):
    """MV and MZ sit beside their tree; only VX Ace ships a container."""
    from bokucho.application.projects import (
        CreateProjectRequest,
        create_project,
        resolve_game_root,
    )
    from bokucho.project import Project

    game = tmp_path / "game-root"
    game.mkdir()
    build_mv(game)
    exe = game / "Game.exe"
    exe.write_bytes(b"MZ")
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "launcher", "ja", "ja", "rpgmaker")
    )
    engine = project.config.engine.model_copy(
        update={"game_path": exe.resolve().as_posix()}
    )
    project.save_config(project.config.model_copy(update={"engine": engine}))
    project = Project.load(project.root)

    assert resolve_game_root(project) == game.resolve()


@pytest.mark.parametrize(
    ("name", "engine"), [("Game.rgssad", "rpgmaker-xp"), ("Game.rgss2a", "rpgmaker-vx")]
)
def test_a_packed_xp_or_vx_game_is_supported(tmp_path, name, engine):
    (tmp_path / name).write_bytes(b"RGSSAD\0\x01")
    found = detect(tmp_path)[0]
    assert (found.engine, found.supported) == (engine, True)


@pytest.mark.parametrize(
    ("relative", "variant", "packed"),
    [
        ("Data/System.rxdata", Variant.XP, False),
        ("Game.rgssad", Variant.XP, True),
        ("Data/System.rvdata", Variant.VX, False),
        ("Game.rgss2a", Variant.VX, True),
    ],
)
def test_sniff_finds_xp_and_vx_games(tmp_path, relative, variant, packed):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x04\x080")
    layout = sniff(tmp_path)
    assert (layout.variant, layout.archive) == (variant, target if packed else None)
    if packed:
        assert sniff(target).archive == target


@pytest.mark.parametrize(
    ("fixture", "archive", "engine", "system"),
    [
        ("build_archive", "Game.rgss3a", "rpgmaker-vx-ace", "Data\\System.rvdata2"),
        ("build_archive_v1", "Game.rgssad", "rpgmaker-xp", "Data\\System.rxdata"),
    ],
)
def test_pictures_inside_a_packed_game_named_by_its_folder_are_inventoried(
    tmp_path, request, fixture, archive, engine, system
):
    pytest.importorskip("rubymarshal")
    import io

    from PIL import Image

    from bokucho.application.assets import extract_images
    from bokucho.application.projects import CreateProjectRequest, create_project, resolve_game_root
    from bokucho.infrastructure.factories import make_engine_adapter
    from bokucho.project import Project

    png = io.BytesIO()
    Image.new("RGB", (1, 1)).save(png, "PNG")
    game = tmp_path / "game"
    game.mkdir()
    request.getfixturevalue(fixture)(
        game / archive, {system: b"\x04\x080", "Graphics\\Pictures\\title.png": png.getvalue()}
    )
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "p", "ja", "zh-Hant", engine)
    )
    engine_config = project.config.engine.model_copy(
        update={"game_path": game.resolve().as_posix()}
    )
    project.save_config(project.config.model_copy(update={"engine": engine_config}))
    project = Project.load(project.root)
    make_engine_adapter(project).extract(game)

    unpacked = (project.root / "unpacked" / "Game").resolve()
    assert resolve_game_root(project).resolve() == unpacked
    assert [entry["image"] for entry in extract_images(project).entries] == [
        "Graphics/Pictures/title.png"
    ]


def test_a_folder_with_no_rpg_maker_game_still_resolves_to_itself(tmp_path):
    from bokucho.application.projects import CreateProjectRequest, create_project, resolve_game_root
    from bokucho.project import Project

    game = tmp_path / "plain"
    game.mkdir()
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "p", "ja", "ja", "rpgmaker-xp")
    )
    engine = project.config.engine.model_copy(
        update={"game_path": game.resolve().as_posix()}
    )
    project.save_config(project.config.model_copy(update={"engine": engine}))
    assert resolve_game_root(Project.load(project.root)).resolve() == game.resolve()


def test_a_legacy_data_file_round_trips_through_load_and_write(tmp_path, ruby):
    pytest.importorskip("rubymarshal")
    from bokucho.engines.rpgmaker_marshal import dump_marshal

    source = tmp_path / "Actors.rxdata"
    source.write_bytes(dump_marshal([None, ruby("RPG::Actor", id=1, name="ハロルド".encode())]))
    destination = tmp_path / "out" / "Actors.rxdata"
    write_data(destination, load_data(source))
    assert destination.read_bytes() == source.read_bytes()


def test_a_legacy_splice_that_did_not_survive_names_the_file(tmp_path, monkeypatch):
    pytest.importorskip("rubymarshal")
    from bokucho.engines import rpgmaker_marshal

    def refuse(data):
        raise ValueError("the translated strings did not survive splicing")

    monkeypatch.setattr(rpgmaker_marshal, "dump_legacy", refuse)
    with pytest.raises(RpgMakerWriteError, match="Actors.rxdata"):
        write_data(tmp_path / "Actors.rxdata", object())
    assert not (tmp_path / "Actors.rxdata").exists()


def test_a_vx_ace_archive_is_not_mistaken_for_xp(tmp_path):
    (tmp_path / "Game.rgss3a").write_bytes(b"RGSSAD\0\x03")
    assert detect(tmp_path)[0].engine == "rpgmaker-vx-ace"


def test_one_unreadable_entry_does_not_cost_the_whole_archive(tmp_path, build_archive):
    pytest.importorskip("rubymarshal")
    from bokucho.engines.rpgmaker_marshal import dump_marshal

    game = tmp_path / "game"
    game.mkdir()
    archive = build_archive(
        game / "Game.rgss3a",
        {
            "Data\\System.rvdata2": dump_marshal(None) + b"\x00" * 64,
            "Data\\Actors.rvdata2": dump_marshal(None) + b"\x00" * 64,
        },
    )
    archive.write_bytes(archive.read_bytes()[:-8])

    events: list = []
    adapter = RpgMakerAdapter(workspace=tmp_path / "unpacked")
    adapter.extract(game / "Game.rgss3a", _Recorder(events))
    unpacked = tmp_path / "unpacked" / "Game" / "Data"
    assert unpacked.is_dir(), "the archive yielded nothing at all"
    assert [e.detail for e in events if e.level is ProgressLevel.WARNING]


class _Recorder:
    def __init__(self, events: list) -> None:
        self._events = events

    def publish(self, event) -> None:
        self._events.append(event)
