"""RPG Maker MV/MZ asset encryption -- a fake header and one XOR."""

import json

import pytest

from bokucho.engines.rpgmv_assets import (
    HEADER,
    decrypt,
    encrypt,
    encryption_key,
    key_for,
    open_bytes,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(40))
KEY = "a24255501e9aea7de2d81f1b2d070d73"


def build_game(root, *, encrypted=True, nested="www"):
    """A game tree with one picture, laid out the way the engine deploys it."""
    base = root / nested if nested else root
    data = base / "data"
    data.mkdir(parents=True)
    (data / "System.json").write_text(
        json.dumps({"hasEncryptedImages": encrypted, "encryptionKey": KEY}),
        encoding="utf-8",
    )
    pictures = base / "img" / "pictures"
    pictures.mkdir(parents=True)
    return pictures


@pytest.mark.parametrize(
    ("nested", "name"), [("www", "title.rpgmvp"), ("", "title.png_")], ids=["mv", "mz"]
)
def test_the_key_is_found_from_the_picture(tmp_path, nested, name):
    pictures = build_game(tmp_path, nested=nested)
    asset = pictures / name
    asset.write_bytes(encrypt(PNG, KEY))
    assert key_for(asset) == KEY


def test_no_key_for_a_picture_outside_any_game(tmp_path):
    stray = tmp_path / "loose.rpgmvp"
    stray.write_bytes(encrypt(PNG, KEY))
    assert key_for(stray) is None


def test_open_bytes_decrypts_an_encrypted_asset(tmp_path):
    pictures = build_game(tmp_path)
    asset = pictures / "title.rpgmvp"
    asset.write_bytes(encrypt(PNG, KEY))
    assert open_bytes(asset) == PNG


def test_open_bytes_passes_a_plain_picture_through(tmp_path):
    pictures = build_game(tmp_path, encrypted=False)
    plain = pictures / "title.png"
    plain.write_bytes(PNG)
    assert open_bytes(plain) == PNG


def test_an_encrypted_asset_is_the_header_then_only_the_first_sixteen_bytes_scrambled():
    encrypted = encrypt(PNG, KEY)
    assert len(HEADER) == 16
    assert encrypted.startswith(HEADER)
    assert encrypted[16 : 16 + 16] != PNG[:16]
    assert encrypted[16 + 16 :] == PNG[16:]


def test_no_key_when_the_game_ships_plain_pictures(tmp_path):
    (tmp_path / "System.json").write_text(
        json.dumps({"hasEncryptedImages": False, "encryptionKey": KEY}), encoding="utf-8"
    )
    assert encryption_key(tmp_path) is None


def test_a_short_asset_does_not_overrun(tmp_path):
    tiny = b"\x89PNG"
    assert decrypt(encrypt(tiny, KEY), KEY) == tiny


def test_no_key_when_the_key_field_is_missing(tmp_path):
    (tmp_path / "System.json").write_text(
        json.dumps({"hasEncryptedImages": True}), encoding="utf-8"
    )
    assert encryption_key(tmp_path) is None
