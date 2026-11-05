"""End-to-end checks against a real RPG Maker MV or MZ game."""

import os
from pathlib import Path

import pytest

from bokucho import images
from bokucho.engines.rpgmaker import RpgMakerAdapter, file_slots, load_data, sniff
from bokucho.engines.rpgmv_assets import ENCRYPTED_SUFFIXES, decrypt, encryption_key

GAME = os.environ.get("BOKUCHO_TEST_RPGMAKER", "")

pytestmark = pytest.mark.skipif(
    not GAME, reason="set BOKUCHO_TEST_RPGMAKER to a real MV/MZ game directory"
)


@pytest.fixture(scope="module")
def game():
    root = Path(GAME)
    if not root.is_dir():
        pytest.skip(f"BOKUCHO_TEST_RPGMAKER is not a directory: {root}")
    return root


def test_the_layout_is_recognized(game):
    layout = sniff(game)
    assert (layout.data_dir / "System.json").is_file()


def test_extraction_finds_a_substantial_amount_of_text(game):
    units = RpgMakerAdapter().extract(game)
    assert len(units) > 100
    assert all(unit.id for unit in units)
    assert len({unit.id for unit in units}) == len(units), "ids must be unique"


def test_real_maps_hold_messages_that_span_several_commands(game):
    """The merge is not a precaution: real games lean on it heavily."""
    layout = sniff(game)
    multi = single = 0
    for path in sorted(layout.data_dir.glob("Map*.json"))[:20]:
        if path.stem == "MapInfos":
            continue
        for slot in file_slots(path.stem, load_data(path), layout.variant):
            if slot.kind != "dialogue":
                continue
            if "\n" in slot.text:
                multi += 1
            else:
                single += 1
    assert multi + single > 0
    assert multi > 0, "no multi-command message found -- is the walk merging?"


def test_untranslated_injection_reproduces_the_data(game, tmp_path):
    """Injecting a translation that equals the source changes nothing but bytes."""
    adapter = RpgMakerAdapter()
    units = adapter.extract(game)
    sample = [unit for unit in units if unit.engine_ref["file"].startswith("Map")][:50]
    if not sample:
        pytest.skip("no map text in this game")
    for unit in sample:
        unit.target = unit.source
    adapter.inject(sample, game, tmp_path)
    layout = sniff(game)
    for written in tmp_path.rglob("*.json"):
        assert load_data(written) == load_data(layout.data_dir / written.name)


def test_encrypted_assets_open(game):
    layout = sniff(game)
    key = encryption_key(layout.data_dir)
    if key is None:
        pytest.skip("this game ships its pictures in the clear")
    images_dir = layout.data_dir.parent / "img"
    assert images_dir.is_dir(), f"expected an img directory at {images_dir}"
    picture_suffixes = ENCRYPTED_SUFFIXES & images.IMAGE_EXTS
    sample = next(
        (
            path
            for path in images_dir.rglob("*")
            if path.suffix.lower() in picture_suffixes
        ),
        None,
    )
    if sample is None:
        pytest.skip("no encrypted asset found")
    assert decrypt(sample.read_bytes(), key)[:8] == b"\x89PNG\r\n\x1a\n"

    found = images.iter_images(images_dir)
    assert sample in found
    meta = images.inspect(sample)
    assert meta["width"] > 0 and meta["height"] > 0
