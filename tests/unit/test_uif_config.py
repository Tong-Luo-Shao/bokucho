from __future__ import annotations

import json

from bokucho.application.proxy_font import uif_config


def test_uif_loads_the_font_beside_the_game_and_spoofs_nothing() -> None:
    config = json.loads(uif_config("Bokucho-a1b2c3.ttf", "Bokucho-a1b2c3"))
    assert config["injector"]["enable"] is True
    manager = config["font_manager"]
    assert manager["enable"] is True
    assert manager["resource_files"] == ["Bokucho-a1b2c3.ttf"]
    assert manager["spoof_enumeration"]["enable"] is False
    assert manager["spoof_creation"]["enable"] is False
    assert manager["spoof_creation"]["override_face"] == "Bokucho-a1b2c3"
