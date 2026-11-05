from pathlib import Path

from bokucho.application import fonts
from bokucho.engines.livepixelfont import stride


def test_glyphs_reuse_loaded_sizes_within_their_own_face(
    tmp_path: Path, make_font, monkeypatch
) -> None:
    first = make_font(tmp_path / "first.ttf", "First", "甲乙")
    second = make_font(tmp_path / "second.ttf", "Second", "甲乙")
    face = fonts._Face(first, frozenset(map(ord, "甲乙")), None)
    other = fonts._Face(second, frozenset(map(ord, "甲乙")), None)
    load = fonts.ImageFont.truetype
    loaded = []

    def tracked_load(path, size):
        loaded.append((path, size))
        return load(path, size)

    monkeypatch.setattr(fonts.ImageFont, "truetype", tracked_load)
    assert fonts._draw("甲", face, 16, 16) == bytes(stride(16) * 16)
    sizes = len(loaded)
    assert fonts._draw("乙", face, 16, 16) == bytes(stride(16) * 16)
    assert len(loaded) == sizes
    assert fonts._draw("甲", other, 16, 16) == bytes(stride(16) * 16)
    assert len(loaded) == 2 * sizes
