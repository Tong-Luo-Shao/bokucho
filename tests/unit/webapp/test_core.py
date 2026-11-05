"""RED tests for the bridge core: Api, EventBus and the catalogs."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import bokucho
from bokucho.webapp.api import Api

from .conftest import FakeWindow


def test_event_push_embeds_hostile_payload_safely(event_bus, fake_window: FakeWindow) -> None:
    message = 'She said "hi"\nCJK: 你好'

    event_bus.push({"type": "toast", "message": message})

    assert len(fake_window.calls) == 1
    call = fake_window.calls[0]
    prefix = "bokucho.onEvent("
    assert call.startswith(prefix)
    assert call.endswith(")")
    payload = json.loads(call[len(prefix) : -1])
    assert payload["message"] == message



_TRANSLATIONS_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "bokucho"
    / "resources"
    / "translations"
)


def _parsed_message_count(locale: str) -> int:
    root = ET.parse(_TRANSLATIONS_DIR / f"bokucho_{locale}.ts").getroot()
    return sum(len(context.findall("message")) for context in root.findall("context"))


def test_get_catalog_serves_the_parsed_ts_catalog(api: Api) -> None:
    catalog = api.get_catalog("zh_TW")

    assert "error" not in catalog
    assert catalog["Navigation"]["Overview"] == "總覽"
    assert catalog["Navigation"]["Strings"] == "字串"
    assert catalog["Navigation"]["Settings"] == "設定"
    message_count = sum(len(messages) for messages in catalog.values())
    assert message_count == _parsed_message_count("zh_TW")


def test_the_window_calls_the_sink_the_page_registers() -> None:
    package = Path(bokucho.__file__).parent
    assert "bokucho.onEvent(" in (package / "webapp" / "events.py").read_text(encoding="utf-8")
    bridge = (package / "web" / "assets" / "bridge.js").read_text(encoding="utf-8")
    assert "window.bokucho.onEvent = " in bridge
