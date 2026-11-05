"""Tests for Phase 7b automatic erase + typeset (bokucho.render)."""

import io
import json
import os
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from bokucho import render

REGION = (50, 10, 150, 50)


PLATE_BOX = (50, 10, 130, 38)


def _plate(frame_y: int) -> np.ndarray:
    """A dark 200x60 plate: a bright frame line, and two bars below the box."""
    img = Image.new("L", (200, 60), 20)
    d = ImageDraw.Draw(img)
    d.rectangle((0, frame_y, 199, frame_y), fill=250)
    for left in (60, 100):
        d.rectangle((left, 20, left + 19, 41), fill=200)
    return np.array(img)


def _found(lum: np.ndarray, box=PLATE_BOX) -> np.ndarray:
    region = render.padded_box(box, 200, 60)
    return render.build_glyph_mask(lum, region, render.detect_polarity(lum, region))


def test_connected_to_drops_the_frame_and_keeps_a_stroke_past_the_box():
    lum = _plate(frame_y=6)
    mask = _found(lum)
    assert mask[6, 100]
    kept = render.connected_to(mask, PLATE_BOX)
    assert not kept[6, :].any()
    assert kept[25, 65] and kept[25, 105]
    assert kept[40, 65]


def test_a_letter_the_box_was_read_short_of_is_not_taken_for_the_frame():
    lum = np.full((30, 130), 250, dtype=np.uint8)
    lum[3:27, 3:127] = 0
    lum[8:22, 10:26] = 200
    lum[8:22, 40:60] = 200
    core = (20, 4, 100, 25)
    box = render.padded_box(core, 130, 30)
    assert box[0] > 10
    found = render.lettering_in(lum, box, core, "light-on-dark")
    assert found.kept[15, 20] and found.kept[15, 50]
    assert found.frame[1, 50] and not found.kept[1, 50]


def test_a_letter_another_box_was_read_on_is_not_followed_into_it():
    lum = np.zeros((30, 130), dtype=np.uint8)
    lum[8:22, 40:56] = 200
    lum[8:22, 58:74] = 200
    mine, theirs = (38, 7, 61, 24), (53, 6, 91, 25)
    box = render.padded_box(mine, 130, 30)
    assert box[2] < 74
    found = render.lettering_in(lum, box, mine, "light-on-dark", others=[theirs])
    assert not found.kept[:, box[2]:].any()


def test_connected_to_drops_a_frame_line_that_grazes_the_box():
    lum = _plate(frame_y=PLATE_BOX[1])
    kept = render.connected_to(_found(lum), PLATE_BOX)
    assert not kept[PLATE_BOX[1], :].any()
    assert kept[25, 65] and kept[40, 65]


def _rgb_button(core=(230, 225, 215), edge=(40, 40, 40), bg=(210, 205, 195)):
    """RGBA-less RGB button: bars with dark edge (2px) and bright core."""
    img = Image.new("RGB", (200, 60), bg)
    d = ImageDraw.Draw(img)
    for x in (60, 100):
        d.rectangle((x, 18, x + 19, 41), fill=edge)
        d.rectangle((x + 2, 20, x + 17, 39), fill=core)
    return np.array(img)


def _ink(rgb: np.ndarray, region=REGION) -> render.Ink:
    lum = np.array(Image.fromarray(rgb).convert("L"))
    polarity = render.detect_polarity(lum, region)
    glyph = render.build_glyph_mask(lum, region, polarity)
    return render.measure_ink(rgb, lum, glyph, render.panel_luminance(lum, region))


def test_measure_ink_keeps_a_real_outline():
    """Bright lettering outlined in black on a dark panel keeps its outline."""
    rgb = np.array(Image.new("RGB", (200, 60), (60, 58, 55)))
    for x in (60, 100):
        rgb[18:42, x:x + 20] = (8, 8, 8)
        rgb[20:40, x + 2:x + 18] = (240, 238, 230)
    ink = _ink(rgb)
    assert ink.fill[0] > 200
    assert ink.stroke == (8, 8, 8)


def test_measure_ink_of_solid_dark_text_has_no_outline():
    rgb = np.array(Image.new("RGB", (200, 60), (210, 205, 195)))
    rgb[18:42, 60:80] = (40, 40, 40)
    rgb[18:42, 100:120] = (40, 40, 40)
    ink = _ink(rgb)
    assert ink.fill == (40, 40, 40)
    assert ink.stroke is None
    assert ink.bbox == (60, 18, 120, 42)


def test_a_few_grains_of_the_panel_past_it_are_no_outline():
    """Light lettering on a textured plate: a darker grain or two is no ring."""
    rgb = np.array(Image.new("RGB", (200, 60), (63, 63, 63)))
    rgb[18:42, 60:80] = (150, 150, 150)
    rgb[18:42, 100:120] = (150, 150, 150)
    rgb[17, 65] = rgb[42, 110] = rgb[30, 59] = (40, 40, 40)
    assert _ink(rgb).stroke is None


def _ascii_font() -> str:
    """A real TTF with ASCII coverage, cross-platform (Windows / ubuntu CI)."""
    for cand in (r"C:\Windows\Fonts\arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(cand).is_file():
            return cand
    pytest.skip("no test font available")


def test_fit_font_matches_height_and_keeps_size_when_width_fits():
    font = _ascii_font()
    fit = render.fit_font("Hi", font, target_h=18, max_w=500)
    th = fit.bbox[3] - fit.bbox[1]
    assert abs(th - 18) <= 2
    assert not fit.needs_manual


def test_fit_font_never_outgrows_the_lettering_it_replaces():
    """A line taller than the original cannot be centred on it."""
    font = _ascii_font()
    for stroke in (0, 1, 2):
        for text in ("Hi", "Ag", "Åj"):
            for target in range(9, 30):
                fit = render.fit_font(text, font, target, 500, stroke)
                grown = fit.bbox[3] - fit.bbox[1]
                case = (text, stroke, target, grown, fit.size)
                assert grown <= target or fit.size <= 8, case
                taller = render.fit_font(text, font, target + 4, 500, stroke)
                assert taller.size >= fit.size, case


def _drawn_rows(text: str, font) -> int:
    """How many rows ``text`` covers at least half of, as lettering is measured."""
    canvas = Image.new("L", (400, 120))
    ImageDraw.Draw(canvas).text((20, 20), text, font=font, fill=255)
    rows = np.nonzero((np.asarray(canvas) >= 128).any(axis=1))[0]
    return int(rows[-1] - rows[0] + 1)


def test_fit_font_is_as_tall_as_the_lettering_measured_the_same_way():
    """The lettering was measured where it is half way from panel to letter."""
    font = next((path for path in render.FONT_PROBE if Path(path).is_file()), None)
    if font is None:
        pytest.skip("no CJK font available")
    for text in ("妖魔大元帥", "狀態"):
        for target in range(9, 30):
            fit = render.fit_font(text, font, target, 500)
            case = (text, target, fit.size)
            assert _drawn_rows(text, fit.font) <= target, case
            bigger = ImageFont.truetype(font, fit.size + 1)
            assert _drawn_rows(text, bigger) > target, case


def test_fit_font_sets_a_translation_at_the_size_of_the_lettering_it_replaces():
    """The size the original is set at, read off the original's own letters."""
    font = next((path for path in render.FONT_PROBE if Path(path).is_file()), None)
    if font is None:
        pytest.skip("no CJK font available")
    for source, translation in (("ニワトリ", "雞"), ("男", "男子"), ("シスター", "修女")):
        for size in (12, 16, 19):
            height = _drawn_rows(source, ImageFont.truetype(font, size))
            fit = render.fit_font(translation, font, height, 500, like=source)
            assert _drawn_rows(source, ImageFont.truetype(font, fit.size)) == height
            assert abs(fit.size - size) <= 1, (source, size, fit.size)


def _inked(text: str, font) -> np.ndarray:
    """``text`` drawn where it covers half a pixel or more, as lettering is found."""
    canvas = Image.new("L", (400, 120))
    ImageDraw.Draw(canvas).text((20, 20), text, font=font, fill=255)
    return np.asarray(canvas) >= 128


def test_lettering_far_lighter_than_the_face_is_set_in_the_lighter_weight():
    """A pixel-wide name is not given the bold face."""
    regular, bold = r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"
    if not (Path(regular).is_file() and Path(bold).is_file()):
        pytest.skip("needs both weights of Microsoft JhengHei")
    for face in (regular, bold):
        original = _inked("闇王", ImageFont.truetype(face, 18))
        rows = np.nonzero(original.any(axis=1))[0]
        height = int(rows[-1] - rows[0] + 1)
        assert render.matching_weight(bold, "闇王", original, height) == face


def test_lettering_near_the_face_in_weight_keeps_the_face():
    """A name that reads as bold keeps the bold face even where the regular one counts closer."""
    regular, bold = r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\msjhbd.ttc"
    if not (Path(regular).is_file() and Path(bold).is_file()):
        pytest.skip("needs both weights of Microsoft JhengHei")
    original = _inked("精霊", ImageFont.truetype(regular, 18))
    rows = np.nonzero(original.any(axis=1))[0]
    height = int(rows[-1] - rows[0] + 1)
    assert render.matching_weight(bold, "精霊", original, height) == bold


def test_boxes_read_off_one_line_are_set_as_that_line():
    boxes, lines, sources = render.one_line_each(
        [(38, 7, 61, 24), (53, 6, 91, 25)], ["鎮", "上的人"], ["町", "の人"]
    )
    assert boxes == [(38, 6, 91, 25)]
    assert lines == ["鎮上的人"] and sources == ["町の人"]
    assert render.one_line_each(
        [(0, 0, 40, 20), (44, 0, 80, 20)], ["Dark", "King"], ["闇", "王"]
    )[1] == ["Dark King"]


def test_labels_apart_on_one_row_stay_apart():
    boxes = [(10, 5, 36, 31), (243, 5, 269, 31)]
    assert render.one_line_each(boxes, ["速", "遲"], ["速", "遅"]) == (
        boxes, ["速", "遲"], ["速", "遅"]
    )


def test_fit_font_shrinks_for_width_and_flags_below_floor():
    font = _ascii_font()
    wide = "WWWWWWWWWWWWWWWWWWWW"
    fit = render.fit_font(wide, font, target_h=30, max_w=60)
    assert fit.bbox[2] - fit.bbox[0] <= 60
    assert fit.needs_manual


def test_resolve_model_path_missing_gives_download_command(monkeypatch, tmp_path):
    monkeypatch.setenv("BOKUCHO_LAMA_MODEL", str(tmp_path / "absent.pt"))
    with pytest.raises(render.ModelMissingError) as exc:
        render.resolve_model_path()
    assert "curl -L --create-dirs -o" in str(exc.value)
    assert render.MODEL_URL in str(exc.value)


def test_padded_box_grows_by_its_height_and_stops_at_the_edge():
    assert render.padded_box((50, 20, 150, 60), 200, 100) == (40, 10, 160, 70)
    assert render.padded_box((0, 0, 20, 8), 200, 100) == (0, 0, 24, 12)


def test_resolve_font_prefers_the_project_and_falls_back_to_the_machine(
    monkeypatch,
    tmp_path,
):
    configured = tmp_path / "own.ttf"
    configured.write_bytes(b"font")
    assert render.resolve_font("own.ttf", tmp_path) == str(configured)
    assert render.resolve_font("gone.ttf", tmp_path) is None
    monkeypatch.setattr(render, "FONT_PROBE", (str(configured),))
    assert render.resolve_font(None, tmp_path) == str(configured)
    monkeypatch.setattr(render, "FONT_PROBE", ())
    assert render.resolve_font(None, tmp_path) is None


LAMA = os.environ.get("BOKUCHO_LAMA_MODEL_REAL")


@pytest.mark.skipif(not LAMA, reason="set BOKUCHO_LAMA_MODEL_REAL to big-lama.pt")
def test_real_lama_inpaints_only_inside_mask():
    rgb = _rgb_button()
    lum = np.array(Image.fromarray(rgb).convert("L"))
    glyph = render.build_glyph_mask(lum, REGION, "dark-on-light")
    mask = render.dilate(glyph)
    fn = render.make_inpaint_fn(Path(LAMA))
    out = fn(rgb, mask)
    assert out.shape == rgb.shape and out.dtype == np.uint8
    assert np.array_equal(out[~mask], rgb[~mask])
    assert (np.abs(out[mask].astype(int) - rgb[mask].astype(int)) > 10).any()


class _Picture(NamedTuple):
    """A picture, what to work it from, and where its draft goes."""

    source: Path
    manifest: dict
    destination: Path


def _order(tmp_path, region=(50, 10, 150, 50), style=None) -> _Picture:
    source = tmp_path / "game" / "btn.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_rgb_button()).convert("RGBA").save(source)
    manifest = {"image": "btn.png", "width": 200, "height": 60}
    if region is not None:
        manifest["region"] = list(region)
    if style:
        manifest["style"] = style
    return _Picture(source, manifest, tmp_path / "out" / "btn.png")


def _render(picture: _Picture, target, font, inpaint):
    return render.render_image(
        picture.source, picture.manifest, target, font, inpaint, picture.destination
    )


def _fake_inpaint(rgb, mask):
    out = rgb.copy()
    out[mask] = (210, 205, 195)
    return out


def test_render_order_renders_and_writes_draft(tmp_path):
    d = _order(tmp_path)
    result = _render(d, "OK", _ascii_font(), _fake_inpaint)
    assert result.status == "rendered"
    out = Image.open(d.destination)
    assert out.size == (200, 60) and out.mode == "RGBA"
    orig = np.array(Image.open(d.source).convert("RGB"))
    assert not np.array_equal(np.array(out.convert("RGB")), orig)


def test_render_writes_a_jpeg_back_as_a_jpeg(tmp_path):
    """JPEG has no alpha channel, and the picture never had one to keep."""
    source = tmp_path / "game" / "btn.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_rgb_button()).save(source, quality=95)
    destination = tmp_path / "out" / "btn.jpg"
    result = render.render_image(
        source,
        {"image": "btn.jpg", "width": 200, "height": 60, "region": [50, 10, 150, 50]},
        "OK",
        _ascii_font(),
        _fake_inpaint,
        destination,
    )
    assert result.status == "rendered", result.reason
    out = Image.open(destination)
    assert (out.format, out.mode, out.size) == ("JPEG", "RGB", (200, 60))


def test_a_jpeg_keeps_the_typeset_text_as_sharp_as_a_png(tmp_path):
    """Saved at the encoder's defaults, coloured lettering blurred into its plate."""
    source = tmp_path / "game" / "btn.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_rgb_button()).save(source, quality=95)
    manifest = {
        "image": "btn.jpg",
        "width": 200,
        "height": 60,
        "region": [50, 10, 150, 50],
        "style": {"fill": "#ffd040"},
    }
    drafts = {}
    for suffix in (".png", ".jpg"):
        destination = tmp_path / "out" / f"btn{suffix}"
        result = render.render_image(
            source, manifest, "OK", _ascii_font(), _fake_inpaint, destination
        )
        assert result.status == "rendered", result.reason
        drafts[suffix] = np.asarray(Image.open(destination).convert("RGB"), dtype=float)

    assert np.abs(drafts[".jpg"] - drafts[".png"]).max() < 40


def test_render_leaves_the_frame_around_the_plate_alone(tmp_path):
    """The search widens past the box and finds the frame; the frame is not text."""
    source = tmp_path / "game" / "plate.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_plate(frame_y=6)).convert("RGBA").save(source)
    destination = tmp_path / "out" / "plate.png"
    result = render.render_image(
        source,
        {"image": "plate.png", "regions": [{"box": list(PLATE_BOX), "text": "あ"}]},
        "OK",
        _ascii_font(),
        _fake_inpaint,
        destination,
    )
    assert result.status == "rendered", result.reason
    drawn = np.asarray(Image.open(destination).convert("L"))
    assert (drawn[6] == 250).all()


def test_render_does_not_grow_the_mask_back_onto_the_frame(tmp_path):
    """The skirt of a letter is worth covering; the frame it grazes is not."""
    tight = (50, 18, 130, 45)
    source = tmp_path / "game" / "plate.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_plate(frame_y=tight[1])).convert("RGBA").save(source)
    destination = tmp_path / "out" / "plate.png"
    result = render.render_image(
        source,
        {"image": "plate.png", "regions": [{"box": list(tight), "text": "あ"}]},
        "OK",
        _ascii_font(),
        _fake_inpaint,
        destination,
    )
    assert result.status == "rendered", result.reason
    drawn = np.asarray(Image.open(destination).convert("L"))
    assert (drawn[tight[1]] == 250).all()


def test_erased_lettering_takes_the_opacity_of_its_plate(tmp_path):
    """A see-through plate under opaque lettering: the words leave no outline."""
    lum = _plate(frame_y=6)
    opacity = np.full(lum.shape, 200, dtype=np.uint8)
    opacity[lum == 200] = 255
    picture = Image.fromarray(lum).convert("RGBA")
    picture.putalpha(Image.fromarray(opacity))
    source = tmp_path / "game" / "plate.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    picture.save(source)
    destination = tmp_path / "out" / "plate.png"
    result = render.render_image(
        source,
        {"image": "plate.png", "regions": [{"box": list(PLATE_BOX), "text": "あ"}]},
        "I",
        _ascii_font(),
        _fake_inpaint,
        destination,
    )
    assert result.status == "rendered", result.reason
    drawn = np.asarray(Image.open(destination).getchannel("A"))
    assert (drawn[20:42, 60:80] == 200).all()
    assert (drawn[20:42, 100:120] == 200).all()


def test_render_order_skips_without_region_or_target(tmp_path):
    assert _render(_order(tmp_path, region=None), "OK",
                               _ascii_font(), _fake_inpaint).reason == "no region"
    assert _render(_order(tmp_path), "",
                               _ascii_font(), _fake_inpaint).reason == "no target"


def test_render_order_needs_manual_cases(tmp_path):
    font = _ascii_font()
    r1 = _render(_order(tmp_path), "a\nb", font, _fake_inpaint)
    assert (r1.status, r1.reason) == ("needs_manual", "multiline target")
    r2 = _render(_order(tmp_path), "沒事", font, _fake_inpaint)
    assert (r2.status, r2.reason) == ("needs_manual", "font missing glyphs")


def _encrypted_order(tmp_path, key="a24255501e9aea7de2d81f1b2d070d73") -> tuple[_Picture, str]:
    """A picture stored the way an encrypted MV/MZ game deploys it."""
    from bokucho.engines.rpgmv_assets import encrypt

    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "System.json").write_text(
        json.dumps({"hasEncryptedImages": True, "encryptionKey": key}),
        encoding="utf-8",
    )
    pictures = game / "img" / "pictures"
    pictures.mkdir(parents=True)
    buffer = io.BytesIO()
    Image.fromarray(_rgb_button()).convert("RGBA").save(buffer, format="PNG")
    source = pictures / "btn.rpgmvp"
    source.write_bytes(encrypt(buffer.getvalue(), key))
    manifest = {"image": "btn.rpgmvp", "width": 200, "height": 60, "region": list(REGION)}
    return _Picture(source, manifest, tmp_path / "out" / "btn.rpgmvp"), key


def test_render_reads_and_writes_an_encrypted_picture(tmp_path):
    """The source is decrypted to open it; the draft goes back encrypted too."""
    from bokucho.engines.rpgmv_assets import decrypt

    picture, key = _encrypted_order(tmp_path)
    result = _render(picture, "OK", _ascii_font(), _fake_inpaint)
    assert result.status == "rendered"

    decrypted = decrypt(picture.destination.read_bytes(), key)
    with Image.open(io.BytesIO(decrypted)) as out:
        assert out.format == "PNG"
        assert out.size == (200, 60)
