"""Real pylivemaker round-trip — the Phase 1 acceptance core."""

import os
from collections import defaultdict
from pathlib import Path

import pytest

pytest.importorskip("livemaker", reason="pylivemaker not installed")

from bokucho.engines.livemaker import LiveMakerAdapter  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def _sample_root() -> Path | None:
    env = os.environ.get("BOKUCHO_TEST_LSB")
    if env:
        p = Path(env)
        return p if p.is_dir() else p.parent
    if FIXTURES.is_dir() and any(FIXTURES.rglob("*.lsb")):
        return FIXTURES
    return None


SAMPLE = _sample_root()
pytestmark = pytest.mark.skipif(
    SAMPLE is None,
    reason="no .lsb sample (set BOKUCHO_TEST_LSB or add tests/fixtures/*.lsb)",
)


def test_proxy_injects_chinese_into_real_lsb(tmp_path):
    """Non-CP932 text becomes injectable through the proxy map and decodes back."""
    from bokucho.engines.cp932_proxy import ProxyMap

    plain = LiveMakerAdapter()
    units = plain.extract(SAMPLE)
    unit = next(u for u in units if u.source.strip())
    unit.target = "承蒙您遊玩，貓"

    map_path = tmp_path / "proxy_map.json"
    pm = ProxyMap()
    pm.extend({"您", "貓"}, reserved={ch for u in units for ch in u.source})
    pm.save(map_path)

    adapter = LiveMakerAdapter(proxy_map=map_path)
    out = tmp_path / "out"
    adapter.inject([unit], SAMPLE, out)

    lsb_rel = unit.engine_ref["lsb"]
    raw = [b.text for _, b in plain._open(out / lsb_rel).get_text_blocks()]
    assert all("您" not in t and "貓" not in t for t in raw)
    reparsed = adapter.extract(out)
    assert any(u.source == "承蒙您遊玩，貓" for u in reparsed)


def test_extract_then_inject_is_non_destructive(tmp_path):
    adapter = LiveMakerAdapter()

    units = adapter.extract(SAMPLE)
    assert units, "no text extracted from the sample"

    out = tmp_path / "out"
    adapter.inject(units, SAMPLE, out)

    originals: dict[str, list[str]] = defaultdict(list)
    for unit in units:
        originals[unit.engine_ref["lsb"]].append(unit.source)

    for rel, texts in originals.items():
        produced = out / rel
        assert produced.is_file(), f"inject did not write {rel}"
        reparsed = [block.text for _, block in adapter._open(produced).get_text_blocks()]
        assert reparsed == texts
