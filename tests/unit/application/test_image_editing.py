from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from bokucho.application.assets import image_manifest, image_slug, load_image_units
from bokucho.application.contracts import RasterImage
from bokucho.application.errors import InvalidProject
from bokucho.application.image_editing import (
    _ensure_source_is_preserved,
    load_image_document,
    save_image_document,
)
from bokucho.application.image_typesetting import (
    ImageTextLayer,
    ImageTextStyle,
    load_editor_base,
    load_image_text_layer,
    save_image_text_layer,
)
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.engines.rpgmv_assets import decrypt, encrypt
from bokucho.infrastructure.pillow_image_codec import PillowImageCodec
from bokucho.model import StringFile, TextUnit
from bokucho.project import Project


def image_project(tmp_path: Path, suffix: str = ".png") -> tuple[Project, Path]:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "images", "ja", "zh-Hant", "livemaker")
    )
    source = project.root / "game" / "ui" / f"button{suffix}"
    source.parent.mkdir(parents=True)
    image = Image.new("RGBA", (4, 3), (255, 0, 0, 128))
    if suffix == ".gal":
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        source.write_bytes(buffer.getvalue())
    else:
        image.save(source)
    engine = project.config.engine.model_copy(update={"game_path": "game"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    return Project.load(project.root), source


def png_bytes(color: tuple[int, int, int, int], size: tuple[int, int] = (4, 3)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


_ENCRYPTED_KEY = "a24255501e9aea7de2d81f1b2d070d73"


def encrypted_image_project(tmp_path: Path) -> tuple[Project, Path]:
    """A project whose game ships one encrypted MV/MZ picture."""
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "images", "ja", "zh-Hant", "rpgmaker")
    )
    game = project.root / "game" / "www"
    (game / "data").mkdir(parents=True)
    (game / "data" / "System.json").write_text(
        json.dumps({"hasEncryptedImages": True, "encryptionKey": _ENCRYPTED_KEY}),
        encoding="utf-8",
    )
    pictures = game / "img" / "pictures"
    pictures.mkdir(parents=True)
    source = pictures / "title.rpgmvp"
    source.write_bytes(encrypt(png_bytes((255, 0, 0, 128)), _ENCRYPTED_KEY))
    engine = project.config.engine.model_copy(update={"game_path": "game/www"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    return Project.load(project.root), source


def test_standard_image_edit_writes_overlay_without_touching_source(tmp_path: Path) -> None:
    project, source = image_project(tmp_path)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)
    original = source.read_bytes()

    result = save_image_document(project, document, png_bytes((0, 255, 0, 64)), codec)

    assert result.output_path == project.root / "out" / "ui" / "button.png"
    assert source.read_bytes() == original
    with Image.open(result.output_path) as saved:
        assert saved.size == (4, 3)
        assert saved.mode == "RGBA"
        assert saved.getpixel((0, 0)) == (0, 255, 0, 64)


def test_standard_image_edit_preserves_original_color_mode(tmp_path: Path) -> None:
    project, source = image_project(tmp_path)
    Image.new("RGB", (4, 3), (255, 0, 0)).save(source)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)

    result = save_image_document(project, document, png_bytes((0, 255, 0, 64)), codec)

    assert document.mode == "RGB"
    with Image.open(result.output_path) as saved:
        assert saved.mode == "RGB"
        assert saved.getpixel((0, 0)) == (0, 255, 0)


def test_image_document_rejects_path_escape_and_dimension_changes(tmp_path: Path) -> None:
    project, _source = image_project(tmp_path)
    codec = PillowImageCodec()

    with pytest.raises(InvalidProject, match="Unsafe image path"):
        load_image_document(project, "../outside.png", codec)

    document = load_image_document(project, "ui/button.png", codec)
    with pytest.raises(InvalidProject, match="dimensions"):
        save_image_document(project, document, png_bytes((0, 0, 0, 0), (2, 2)), codec)


def test_gal_palette_alpha_mode_is_preview_only(tmp_path: Path) -> None:
    project, _source = image_project(tmp_path, ".gal")
    codec = PillowImageCodec()
    document = replace(
        load_image_document(project, "ui/button.gal", codec),
        mode="PA",
    )

    assert document.is_editable is False
    with pytest.raises(InvalidProject, match="cannot be edited without loss"):
        save_image_document(project, document, png_bytes((0, 0, 255, 96)), codec)


def test_palette_edit_preserves_untouched_indices(tmp_path: Path) -> None:
    project, source = image_project(tmp_path)
    indexed = Image.new("P", (16, 16))
    palette = [channel for value in range(256) for channel in (value, value, value)]
    indexed.putpalette(palette)
    indexed.putdata(range(256))
    indexed.save(source)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)
    with Image.open(BytesIO(document.image_bytes)) as working:
        edited = working.convert("RGBA")
    edited.putpixel((0, 0), (255, 255, 255, 255))
    buffer = BytesIO()
    edited.save(buffer, format="PNG")

    result = save_image_document(project, document, buffer.getvalue(), codec)

    with Image.open(result.output_path) as saved:
        assert saved.mode == "P"
        assert list(saved.get_flattened_data())[1:] == list(range(1, 256))
        assert saved.getpixel((0, 0)) == 255
        assert saved.getpalette()[:768] == palette


#: A colour a canvas cannot hold as it is: premultiplied, 201 * 10 / 255 reads back as 204.
_FAINT = (201, 77, 13, 10)


def through_a_canvas(picture: Image.Image) -> bytes:
    """``picture`` as a browser canvas hands it back: colour premultiplied by alpha in 8 bits."""
    rgba = picture.convert("RGBA")
    rgba.putdata(
        [
            tuple(min(255, round(round(c * a / 255) * 255 / a)) for c in (r, g, b)) + (a,)
            if a
            else (0, 0, 0, 0)
            for r, g, b, a in rgba.getdata()
        ]
    )
    buffer = BytesIO()
    rgba.save(buffer, format="PNG")
    return buffer.getvalue()


def faint_palette_project(tmp_path: Path) -> tuple[Project, Path]:
    """A picture whose palette holds a partly transparent colour, in its top row."""
    project, source = image_project(tmp_path)
    indexed = Image.new("P", (4, 3), 1)
    indexed.putpalette([0, 0, 0, 40, 40, 40, *_FAINT[:3], 250, 250, 250])
    indexed.info["transparency"] = bytes([0, 255, _FAINT[3], 255])
    indexed.paste(2, (0, 0, 4, 1))
    indexed.save(source, transparency=bytes([0, 255, _FAINT[3], 255]))
    return project, source


def test_an_untouched_faint_palette_picture_saves_as_it_was(tmp_path: Path) -> None:
    project, source = faint_palette_project(tmp_path)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)
    with Image.open(BytesIO(document.editor_image_bytes)) as given:
        edited = through_a_canvas(given)

    result = save_image_document(project, document, edited, codec)

    with Image.open(result.output_path) as saved, Image.open(source) as original:
        assert list(saved.getdata()) == list(original.getdata())


def test_a_faint_palette_colour_painted_through_a_canvas_keeps_its_index(
    tmp_path: Path,
) -> None:
    project, _source = faint_palette_project(tmp_path)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)
    with Image.open(BytesIO(document.editor_image_bytes)) as given:
        painted = given.convert("RGBA")
    painted.putpixel((0, 2), _FAINT)

    result = save_image_document(project, document, through_a_canvas(painted), codec)

    with Image.open(result.output_path) as saved:
        assert saved.getpixel((0, 2)) == 2
        assert saved.getpixel((1, 2)) == 1


def test_faint_pixels_a_canvas_only_passed_on_keep_their_colour(tmp_path: Path) -> None:
    project, source = image_project(tmp_path)
    picture = Image.new("RGBA", (4, 3), _FAINT)
    picture.save(source)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)
    picture.putpixel((3, 2), (0, 255, 0, 255))

    result = save_image_document(project, document, through_a_canvas(picture), codec)

    with Image.open(result.output_path) as saved:
        assert saved.getpixel((0, 0)) == _FAINT
        assert saved.getpixel((3, 2)) == (0, 255, 0, 255)


def test_a_rendered_draft_of_an_indexed_picture_can_be_edited(tmp_path: Path) -> None:
    project, source = faint_palette_project(tmp_path)
    draft = project.root / "out" / "ui" / "button.png"
    draft.parent.mkdir(parents=True)
    Image.new("RGBA", (4, 3), (90, 30, 30, 255)).save(draft)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)

    result = save_image_document(project, document, png_bytes((0, 128, 255, 255)), codec)

    assert document.is_editable
    with Image.open(result.output_path) as saved:
        assert saved.convert("RGBA").getpixel((0, 0)) == (0, 128, 255, 255)


def test_colliding_gal_paths_save_to_their_own_overlay_files(tmp_path: Path) -> None:
    project, source = image_project(tmp_path, ".gal")
    nested = project.root / "game" / "a" / "b.gal"
    literal = project.root / "game" / "a__b.gal"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(source.read_bytes())
    literal.write_bytes(source.read_bytes())
    codec = PillowImageCodec()

    nested_result = save_image_document(
        project,
        load_image_document(project, "a/b.gal", codec),
        png_bytes((255, 0, 0, 128)),
        codec,
    )
    literal_result = save_image_document(
        project,
        load_image_document(project, "a__b.gal", codec),
        png_bytes((0, 0, 255, 128)),
        codec,
    )

    assert nested_result.output_path != literal_result.output_path
    with Image.open(nested_result.output_path) as nested_saved:
        assert nested_saved.getpixel((0, 0)) == (255, 0, 0, 128)
    with Image.open(literal_result.output_path) as literal_saved:
        assert literal_saved.getpixel((0, 0)) == (0, 0, 255, 128)


def test_encrypted_picture_edit_round_trips(tmp_path: Path) -> None:
    project, source = encrypted_image_project(tmp_path)
    codec = PillowImageCodec()
    document = load_image_document(project, "img/pictures/title.rpgmvp", codec)
    original = source.read_bytes()

    result = save_image_document(project, document, png_bytes((0, 255, 0, 64)), codec)

    assert result.output_path == project.root / "out" / "img" / "pictures" / "title.rpgmvp"
    assert source.read_bytes() == original
    decrypted = decrypt(result.output_path.read_bytes(), _ENCRYPTED_KEY)
    with Image.open(BytesIO(decrypted)) as saved:
        assert saved.size == (4, 3)
        assert saved.getpixel((0, 0)) == (0, 255, 0, 64)


def test_encrypted_picture_survives_reopening_after_a_save(tmp_path: Path) -> None:
    """A second edit session reads back its own encrypted overlay."""
    project, _source = encrypted_image_project(tmp_path)
    codec = PillowImageCodec()
    document = load_image_document(project, "img/pictures/title.rpgmvp", codec)
    save_image_document(project, document, png_bytes((0, 255, 0, 64)), codec)

    reopened = load_image_document(project, "img/pictures/title.rpgmvp", codec)
    result = save_image_document(project, reopened, png_bytes((0, 0, 255, 64)), codec)

    decrypted = decrypt(result.output_path.read_bytes(), _ENCRYPTED_KEY)
    with Image.open(BytesIO(decrypted)) as saved:
        assert saved.getpixel((0, 0)) == (0, 0, 255, 64)


def test_encoding_an_encrypted_suffix_without_a_resolvable_key_fails() -> None:
    codec = PillowImageCodec()
    image = RasterImage(png_bytes((255, 0, 0, 128)), 4, 3, "RGBA")

    with pytest.raises(ValueError, match="No encryption key"):
        codec.encode(image, ".rpgmvp", "RGBA", source=Path("loose.rpgmvp"))


def test_image_slugs_are_distinct_at_separator_boundaries() -> None:
    paths = ("a/b.gal", "a__b.gal", "a_/b.gal", "a/_b.gal")

    assert len({image_slug(path) for path in paths}) == len(paths)


def test_gal_path_with_fnmatch_characters_exports_exact_asset(tmp_path: Path) -> None:
    project, source = image_project(tmp_path, ".gal")
    pattern_named = project.root / "game" / "ui" / "[x].gal"
    competing = project.root / "game" / "ui" / "x.gal"
    pattern_named.write_bytes(source.read_bytes())
    competing.write_bytes(source.read_bytes())
    codec = PillowImageCodec()

    result = save_image_document(
        project,
        load_image_document(project, "ui/[x].gal", codec),
        png_bytes((0, 255, 0, 128)),
        codec,
    )

    assert result.output_path == project.root / "out" / "ui" / "[x].gal"
    assert not (project.root / "out" / "ui" / "x.gal").exists()
    assert competing.read_bytes() == pattern_named.read_bytes()


def test_overlay_aliasing_game_source_is_rejected(new_project) -> None:
    project = new_project("images")
    source = project.root / "out" / "ui" / "button.png"
    source.parent.mkdir(parents=True)
    Image.new("RGBA", (4, 3), (255, 0, 0, 128)).save(source)
    engine = project.config.engine.model_copy(update={"game_path": "out"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    project = Project.load(project.root)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)
    original = source.read_bytes()

    with pytest.raises(InvalidProject, match="game source"):
        save_image_document(project, document, png_bytes((0, 255, 0, 128)), codec)

    assert source.read_bytes() == original


def test_source_replacement_while_editor_is_open_is_rejected(tmp_path: Path) -> None:
    project, source = image_project(tmp_path)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)
    Image.new("RGBA", (4, 3), (0, 0, 255, 128)).save(source)

    with pytest.raises(InvalidProject, match="changed since the editor was opened"):
        save_image_document(project, document, png_bytes((0, 255, 0, 128)), codec)


def test_overlay_created_while_editor_is_open_is_rejected(tmp_path: Path) -> None:
    project, _source = image_project(tmp_path)
    codec = PillowImageCodec()
    output = project.root / "out" / "ui" / "button.png"
    document = load_image_document(project, "ui/button.png", codec)
    external = png_bytes((255, 255, 0, 128))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(external)

    with pytest.raises(InvalidProject, match="changed since the editor was opened"):
        save_image_document(project, document, png_bytes((0, 255, 0, 128)), codec)

    assert output.read_bytes() == external


def test_high_bit_depth_image_edit_is_rejected_without_overwrite(tmp_path: Path) -> None:
    project, source = image_project(tmp_path)
    high_depth = Image.new("I;16", (4, 3))
    high_depth.putdata([65535] * 12)
    high_depth.save(source)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/button.png", codec)

    assert document.mode.startswith("I;16")
    assert document.is_editable is False
    with pytest.raises(InvalidProject, match="cannot be edited without loss"):
        save_image_document(project, document, document.image_bytes, codec)
    assert not (project.root / "out" / "ui" / "button.png").exists()


def test_jpeg_edit_uses_high_fidelity_encoding_and_preserves_metadata(tmp_path: Path) -> None:
    project, _source = image_project(tmp_path)
    source = project.root / "game" / "ui" / "photo.jpg"
    image = Image.new("RGB", (32, 32))
    image.putdata(
        [
            ((x * 37) % 256, (y * 53) % 256, ((x + y) * 29) % 256)
            for y in range(32)
            for x in range(32)
        ]
    )
    exif = Image.Exif()
    exif[0x010E] = "Bokucho image editor"
    icc_profile = b"test-icc-profile"
    image.save(source, quality=95, subsampling=0, exif=exif, icc_profile=icc_profile)
    codec = PillowImageCodec()
    document = load_image_document(project, "ui/photo.jpg", codec)

    result = save_image_document(project, document, document.image_bytes, codec)

    with Image.open(result.output_path) as saved:
        assert saved.quantization is not None
        assert max(saved.quantization[0]) <= 15
        assert saved.getexif()[0x010E] == "Bokucho image editor"
        assert saved.info["icc_profile"] == icc_profile


def test_cmyk_image_is_preview_only(tmp_path: Path) -> None:
    project, _source = image_project(tmp_path)
    source = project.root / "game" / "ui" / "print.jpg"
    Image.new("CMYK", (4, 3), (10, 20, 30, 40)).save(source)
    codec = PillowImageCodec()

    document = load_image_document(project, "ui/print.jpg", codec)

    assert document.mode == "CMYK"
    assert document.is_editable is False
    with pytest.raises(InvalidProject, match="cannot be edited without loss"):
        save_image_document(project, document, document.image_bytes, codec)


def test_image_output_must_resolve_inside_project(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    source = project_root / "game.png"
    source.write_bytes(b"source")

    with pytest.raises(InvalidProject, match="outside the project"):
        _ensure_source_is_preserved(tmp_path / "outside.png", source, project_root)



def _project(tmp_path: Path) -> Project:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "images", "ja", "zh-Hant", "livemaker")
    )
    source = project.root / "game" / "ui" / "button.png"
    source.parent.mkdir(parents=True)
    Image.new("RGBA", (120, 48), "white").save(source)
    engine = project.config.engine.model_copy(update={"game_path": "game"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    project.save_string_file(
        StringFile(
            source_file="images",
            units=[
                TextUnit(
                    id="img:ui/button.png",
                    source="スタート",
                    kind="image",
                    engine_ref={"image": "ui/button.png", "format": "PNG"},
                )
            ],
        ),
        "images.json",
    )
    return Project.load(project.root)


def _png_bytes(color: str = "white") -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (120, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _write_overlay(project: Project, data: bytes | None = None) -> Path:
    output = project.root / "out" / "ui" / "button.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data or _png_bytes())
    return output


def test_image_text_layer_round_trips_with_editor_base(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_overlay(project)
    layer = ImageTextLayer(
        source="スタート",
        target="開始",
        region=(8, 6, 112, 42),
        style=ImageTextStyle(
            font_family="IBM Plex Sans TC",
            font_size=22,
            fill="#FF155EEF",
            stroke="#FF101828",
            stroke_width=1,
            background="#FFFFFFFF",
            alignment="center",
        ),
    )

    saved = save_image_text_layer(
        project,
        "ui/button.png",
        layer,
        base_png=_png_bytes(),
        source_fingerprint="source-sha256",
    )

    assert saved.target == "開始"
    assert load_image_text_layer(
        project,
        "ui/button.png",
        source_fingerprint="source-sha256",
    ) == layer
    assert load_editor_base(
        project,
        "ui/button.png",
        source_fingerprint="source-sha256",
    ) == _png_bytes()
    assert (
        load_editor_base(
            project,
            "ui/button.png",
            source_fingerprint="changed-source",
        )
        is None
    )


def test_the_manifest_carries_the_saved_text_region_and_style(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_overlay(project)
    layer = ImageTextLayer(
        source="スタート",
        target="開始",
        region=(8, 6, 112, 42),
        style=ImageTextStyle(font_size=22, alignment="right"),
    )
    save_image_text_layer(
        project,
        "ui/button.png",
        layer,
        base_png=_png_bytes(),
        source_fingerprint="source-sha256",
    )

    manifest = image_manifest(load_image_units(project).by_id("img:ui/button.png"))

    assert manifest["region"] == [8, 6, 112, 42]
    assert manifest["style"]["font_size"] == 22
    assert manifest["style"]["alignment"] == "right"
