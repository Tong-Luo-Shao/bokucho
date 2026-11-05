"""The RGSSAD v3 container, which is where a VX Ace game keeps its data."""

import pytest

from bokucho.engines.rgssad import (
    MAGIC,
    EntryUnreadable,
    NotAnArchive,
    entries,
    read,
    rebuild,
)


def test_every_entry_is_listed(tmp_path, build_archive):
    archive = build_archive(
        tmp_path / "Game.rgss3a",
        {"Data\\Actors.rvdata2": b"actors", "Data\\Map001.rvdata2": b"a map"},
    )
    assert [entry.name for entry in entries(archive)] == [
        "Data\\Actors.rvdata2",
        "Data\\Map001.rvdata2",
    ]


def test_a_file_comes_back_as_it_went_in(tmp_path, build_archive):
    payload = bytes(range(256)) * 3
    archive = build_archive(tmp_path / "Game.rgss3a", {"Data\\System.rvdata2": payload})
    [entry] = entries(archive)
    assert read(archive, entry) == payload


def test_an_empty_archive_lists_nothing(tmp_path, build_archive):
    archive = build_archive(tmp_path / "Game.rgss3a", {})
    assert entries(archive) == []


def test_something_that_is_not_an_archive_is_refused(tmp_path):
    plain = tmp_path / "Game.exe"
    plain.write_bytes(b"MZ" + bytes(64))
    with pytest.raises(NotAnArchive, match="Game.exe"):
        entries(plain)


def test_an_unknown_archive_version_is_refused(tmp_path):
    other = tmp_path / "Game.rgss2a"
    other.write_bytes(MAGIC + bytes([2]) + bytes(32))
    with pytest.raises(NotAnArchive, match="Game.rgss2a"):
        entries(other)


def test_a_file_that_is_just_the_magic_is_refused(tmp_path):
    just_magic = tmp_path / "Truncated.rgss3a"
    just_magic.write_bytes(MAGIC)
    with pytest.raises(NotAnArchive, match="Truncated.rgss3a"):
        entries(just_magic)


def test_a_header_missing_its_seed_is_refused(tmp_path):
    no_seed = tmp_path / "NoSeed.rgss3a"
    no_seed.write_bytes(MAGIC + bytes([3]))
    with pytest.raises(NotAnArchive, match="NoSeed.rgss3a"):
        entries(no_seed)


def test_an_entry_table_cut_off_mid_record_is_refused(tmp_path, build_archive):
    archive = build_archive(tmp_path / "Game.rgss3a", {"Data\\Actors.rvdata2": b"actors"})
    header = len(MAGIC) + 1 + 4
    cut = tmp_path / "Cut.rgss3a"
    cut.write_bytes(archive.read_bytes()[: header + 8])
    with pytest.raises(NotAnArchive, match="Cut.rgss3a"):
        entries(cut)


def test_a_name_cut_off_mid_character_is_refused(tmp_path, build_archive):
    archive = build_archive(
        tmp_path / "Game.rgss3a", {"Data\\マップ001.rvdata2": b"a map"}
    )
    blob = archive.read_bytes()
    cut = tmp_path / "CutName.rgss3a"
    name_starts = len(MAGIC) + 1 + 4 + 16
    name_len = len("Data\\マップ001.rvdata2".encode())
    for size in range(name_starts + 1, name_starts + name_len):
        cut.write_bytes(blob[:size])
        with pytest.raises(NotAnArchive, match="CutName.rgss3a"):
            entries(cut)


def test_a_name_that_is_not_a_name_is_refused(tmp_path, build_archive, archive_key_bytes):
    archive = build_archive(tmp_path / "Game.rgss3a", {"Data\\a.rvdata2": b"x"})
    blob = bytearray(archive.read_bytes())
    name_starts = len(MAGIC) + 1 + 4 + 16
    blob[name_starts] = 0xFF ^ archive_key_bytes[0]
    broken = tmp_path / "Garbled.rgss3a"
    broken.write_bytes(bytes(blob))
    with pytest.raises(NotAnArchive, match="Garbled.rgss3a"):
        entries(broken)


def test_an_entry_running_past_the_end_is_refused_by_name(tmp_path, build_archive):
    archive = build_archive(
        tmp_path / "Game.rgss3a", {"Data\\Actors.rvdata2": b"actors"}
    )
    truncated = tmp_path / "Short.rgss3a"
    truncated.write_bytes(archive.read_bytes()[:-3])
    [entry] = entries(truncated)
    with pytest.raises(EntryUnreadable, match="Actors"):
        read(truncated, entry)


def test_a_rebuilt_archive_carries_the_replacement_and_the_rest_unchanged(
    tmp_path, build_archive
):
    archive = build_archive(
        tmp_path / "Game.rgss3a",
        {
            "Data\\Map001.rvdata2": b"a map",
            "Graphics\\Pictures\\title.png": b"picture " * 50,
            "Audio\\BGM\\theme.ogg": b"music " * 80,
        },
    )
    rebuilt = tmp_path / "out" / "Game.rgss3a"
    rebuilt.parent.mkdir()
    rebuild(
        archive,
        {"Data/Map001.rvdata2": b"a translated map", "Graphics/Pictures/title.png": b"new"},
        rebuilt,
    )
    contents = {entry.name: read(rebuilt, entry) for entry in entries(rebuilt)}
    assert contents == {
        "Data\\Map001.rvdata2": b"a translated map",
        "Graphics\\Pictures\\title.png": b"new",
        "Audio\\BGM\\theme.ogg": b"music " * 80,
    }
    [theme] = [entry for entry in entries(archive) if entry.name.endswith("theme.ogg")]
    [moved] = [entry for entry in entries(rebuilt) if entry.name.endswith("theme.ogg")]
    original = archive.read_bytes()[theme.offset : theme.offset + theme.size]
    assert rebuilt.read_bytes()[moved.offset : moved.offset + moved.size] == original
    assert moved.key == theme.key


def test_a_replacement_matches_its_entry_whatever_the_case(tmp_path, build_archive):
    archive = build_archive(tmp_path / "Game.rgss3a", {"data\\System.rvdata2": b"old"})
    rebuilt = tmp_path / "Rebuilt.rgss3a"
    rebuild(archive, {"Data/System.rvdata2": b"new"}, rebuilt)
    [entry] = entries(rebuilt)
    assert (entry.name, read(rebuilt, entry)) == ("data\\System.rvdata2", b"new")


def test_a_replacement_the_archive_does_not_hold_is_refused(tmp_path, build_archive):
    archive = build_archive(tmp_path / "Game.rgss3a", {"Data\\System.rvdata2": b"old"})
    rebuilt = tmp_path / "Rebuilt.rgss3a"
    with pytest.raises(ValueError, match="Map999"):
        rebuild(archive, {"Data/Map999.rvdata2": b"new"}, rebuilt)
    assert not rebuilt.exists()


def test_every_v1_entry_is_listed_and_reads_back(tmp_path, build_archive_v1):
    files = {
        "Data\\Actors.rxdata": b"actors",
        "Graphics\\Pictures\\タイトル.png": bytes(range(256)) * 3,
    }
    archive = build_archive_v1(tmp_path / "Game.rgssad", files)
    found = entries(archive)
    assert [entry.name for entry in found] == list(files)
    assert [read(archive, entry) for entry in found] == list(files.values())


def test_a_rebuilt_v1_archive_changes_only_the_replaced_size_and_data(tmp_path, build_archive_v1):
    archive = build_archive_v1(
        tmp_path / "Game.rgss2a",
        {
            "Data\\Map001.rvdata": b"a map",
            "Data\\Actors.rvdata": b"old",
            "Audio\\BGM\\t.mid": b"music " * 80,
        },
    )
    rebuilt = tmp_path / "out.rgss2a"
    rebuild(archive, {"Data/Actors.rvdata": b"a much longer translated actors file"}, rebuilt)
    contents = {entry.name: read(rebuilt, entry) for entry in entries(rebuilt)}
    assert contents == {
        "Data\\Map001.rvdata": b"a map",
        "Data\\Actors.rvdata": b"a much longer translated actors file",
        "Audio\\BGM\\t.mid": b"music " * 80,
    }
    [old] = [entry for entry in entries(archive) if entry.name.endswith("Actors.rvdata")]
    before, after = archive.read_bytes(), rebuilt.read_bytes()
    assert after[: old.offset - 4] == before[: old.offset - 4]
    assert after.endswith(before[old.offset + old.size :])


def test_a_v1_replacement_the_archive_does_not_hold_is_refused(tmp_path, build_archive_v1):
    archive = build_archive_v1(tmp_path / "Game.rgssad", {"Data\\System.rxdata": b"old"})
    with pytest.raises(ValueError, match="Map999"):
        rebuild(archive, {"Data/Map999.rxdata": b"new"}, tmp_path / "out.rgssad")
    assert not (tmp_path / "out.rgssad").exists()


def test_a_v1_entry_running_past_the_end_is_refused_by_name(tmp_path, build_archive_v1):
    archive = build_archive_v1(tmp_path / "Game.rgssad", {"Data\\Actors.rxdata": b"actors"})
    short = tmp_path / "Short.rgssad"
    short.write_bytes(archive.read_bytes()[:-3])
    [entry] = entries(short)
    with pytest.raises(EntryUnreadable, match="Actors"):
        read(short, entry)


def test_a_v1_name_cut_off_is_refused(tmp_path, build_archive_v1):
    archive = build_archive_v1(tmp_path / "Game.rgssad", {"Data\\Actors.rxdata": b"actors"})
    cut = tmp_path / "Cut.rgssad"
    cut.write_bytes(archive.read_bytes()[: 8 + 4 + 5])
    with pytest.raises(NotAnArchive, match="Cut.rgssad"):
        entries(cut)
