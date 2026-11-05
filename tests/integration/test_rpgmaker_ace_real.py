"""VX Ace against shipped games, which is where the design's numbers came from."""

import os
from pathlib import Path

import pytest

pytest.importorskip("rubymarshal")

from bokucho.engines import rgssad  # noqa: E402
from bokucho.engines.rpgmaker import (  # noqa: E402
    RpgMakerAdapter,
    Variant,
    sniff,
)
from bokucho.engines.rpgmaker_marshal import (  # noqa: E402
    dump_marshal,
    load_marshal,
    view,
)

GAME = os.environ.get("BOKUCHO_TEST_ACE", "")

pytestmark = pytest.mark.skipif(
    not GAME, reason="set BOKUCHO_TEST_ACE to a real VX Ace game directory"
)


@pytest.fixture(scope="module")
def game():
    root = Path(GAME)
    if not root.is_dir():
        pytest.skip(f"BOKUCHO_TEST_ACE is not a directory: {root}")
    return root


def test_the_layout_is_recognized(game):
    layout = sniff(game)
    assert layout.variant is Variant.ACE
    assert layout.archive is not None or layout.data_dir.is_dir()


def test_most_data_files_survive_a_round_trip(game, tmp_path):
    """The design measured 96.3% byte-identical and 99.65% readable."""
    layout = sniff(game)
    if layout.archive is None:
        blobs = {
            path.stem: path.read_bytes()
            for path in sorted(layout.data_dir.glob("*.rvdata2"))
        }
    else:
        blobs = {
            Path(entry.name).stem: rgssad.read(layout.archive, entry)
            for entry in rgssad.entries(layout.archive)
            if entry.name.lower().endswith(".rvdata2")
        }
    blobs = {stem: blob for stem, blob in blobs.items() if stem != "Scripts"}
    if not blobs:
        pytest.skip("no .rvdata2 files in this game")

    identical = preserved = broken = 0
    for blob in blobs.values():
        try:
            parsed = load_marshal(blob)
            written = dump_marshal(parsed)
        except Exception:  # noqa: BLE001 - the point is which files fail at all
            broken += 1
            continue
        if written == blob:
            identical += 1
        elif repr(load_marshal(written)) == repr(parsed):
            preserved += 1
        else:
            broken += 1

    total = len(blobs)
    assert broken / total < 0.02, f"{broken} of {total} files did not survive"
    assert (identical + preserved) / total > 0.98
    assert identical / total > 0.80, "byte-identity collapsed; the codec changed"


def test_a_real_map_holds_messages_that_span_several_commands(game):
    """The merge is what the whole engine rests on; real data leans on it."""
    layout = sniff(game)
    if layout.archive is None:
        maps = sorted(layout.data_dir.glob("Map0*.rvdata2"))[:10]
        blobs = [path.read_bytes() for path in maps]
    else:
        blobs = [
            rgssad.read(layout.archive, entry)
            for entry in rgssad.entries(layout.archive)
            if "/map0" in entry.name.lower().replace("\\", "/")
        ][:10]
    if not blobs:
        pytest.skip("no map files in this game")

    from bokucho.engines.rpgmaker import file_slots

    multi = single = 0
    for blob in blobs:
        try:
            payload = view(load_marshal(blob))
        except Exception:  # noqa: BLE001 - a broken map is counted elsewhere
            continue
        for slot in file_slots("Map001", payload, Variant.ACE):
            if slot.kind != "dialogue":
                continue
            if "\n" in slot.text:
                multi += 1
            else:
                single += 1
    assert multi + single > 0, "no dialogue found in any map"
    assert multi > 0, "no multi-command message found -- is the walk merging?"


def test_extraction_finds_text_end_to_end(game, tmp_path):
    units = RpgMakerAdapter(workspace=tmp_path / "unpacked").extract(game)
    assert len(units) > 100
    assert len({unit.id for unit in units}) == len(units), "ids must be unique"
