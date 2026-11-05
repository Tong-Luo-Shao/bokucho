from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

from bokucho import images, render
from bokucho.application.assets import (
    ocr_regions,
    render_images,
    save_image_units,
    typeset_font,
)
from bokucho.application.errors import InvalidProject
from bokucho.application.image_animation import create_image_animation
from bokucho.application.image_editing import load_image_document
from bokucho.application.image_editor_saving import save_image_editor_document
from bokucho.application.image_typesetting import load_image_text_layer
from bokucho.application.projects import CreateProjectRequest, create_project
from bokucho.infrastructure.pillow_image_codec import PillowImageCodec
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project


def test_application_keeps_regions_already_accepted_by_configured_threshold() -> None:
    answer = json.dumps(
        [{"bbox_2d": [0, 0, 1000, 1000], "text": "文字", "score": 0.85}]
    )

    text, regions = ocr_regions(answer, 100, 50)

    assert text == "文字"
    assert regions == [{"box": [0, 0, 100, 50], "text": "文字", "score": 0.85}]


def _typesetting_in(tmp_path: Path, font: Path, variation: str) -> Project:
    project = create_project(
        CreateProjectRequest(tmp_path / "game", "測試", "ja", "zh-Hant", "livemaker")
    )
    images = project.config.images.model_copy(
        update={"font": font.as_posix(), "variation": variation}
    )
    project.save_config(project.config.model_copy(update={"images": images}))
    return Project.load(project.root)


def test_pictures_are_typeset_in_the_weight_asked_for(tmp_path: Path, make_font) -> None:
    face = make_font(tmp_path / "var.ttf", "Var Sans", "甲乙", weights={"Bold": 700})
    pinned = TTFont(typeset_font(_typesetting_in(tmp_path, face, "Bold")))
    assert "fvar" not in pinned
    assert pinned["OS/2"].usWeightClass == 700


def test_a_weight_the_face_lacks_is_refused_in_words(tmp_path: Path, make_font) -> None:
    face = make_font(tmp_path / "var.ttf", "Var Sans", "甲乙", weights={"Bold": 700})
    with pytest.raises(InvalidProject, match="Heavy"):
        typeset_font(_typesetting_in(tmp_path, face, "Heavy"))


def _button(path: Path, text: str, font: str, size: int) -> list[int]:
    """A dark 80x32 button lettered ``text``; the box the lettering covers."""
    picture = Image.new("RGBA", (80, 32), (30, 30, 40, 255))
    draw = ImageDraw.Draw(picture)
    face = ImageFont.truetype(font, size, index=0)
    draw.text((8, 6), text, font=face, fill=(240, 240, 240, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    picture.save(path)
    return list(draw.textbbox((8, 6), text, font=face))


def test_buttons_of_one_set_are_lettered_at_one_size(tmp_path: Path) -> None:
    """A menu's buttons share one size, each fitted to its own original."""
    font = next((path for path in render.FONT_PROBE if Path(path).is_file()), None)
    if font is None:
        pytest.skip("no CJK font available")
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "測試", "ja", "zh-Hant", "livemaker")
    )
    game = project.root / "game"
    engine = project.config.engine.model_copy(update={"game_path": "game"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    units = []
    for name, source, target, size in (
        ("スキル", "スキル", "技能", 13),
        ("アイテム", "アイテム", "道具", 15),
        ("装備", "システム", "系統", 14),
    ):
        box = _button(game / "parts" / f"{name}.png", source, font, size)
        regions = [{"box": box, "text": source}]
        units.append(
            TextUnit(
                id=f"image:parts/{name}.png", source=source, target=target, status=Status.MT,
                kind="image", engine_ref={"image": f"parts/{name}.png", "regions": regions},
            )
        )
    save_image_units(project, StringFile(source_file="images", units=units))

    run = render_images(
        Project.load(project.root), font, lambda rgb, mask: rgb, codec=PillowImageCodec()
    )

    assert not run.errors
    assert {result.status for result in run.results} == {"rendered"}
    assert len({result.size for result in run.results}) == 1


def _png(color: tuple[int, int, int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGBA", (80, 32), color).save(stream, "PNG")
    return stream.getvalue()


def _skill_picture(
    tmp_path: Path,
    target: str,
    name: str = "skill.png",
    mode: str = "RGBA",
    background: tuple[int, int, int, int] = (30, 30, 40, 255),
) -> Project:
    project = create_project(
        CreateProjectRequest(tmp_path / "project", "測試", "ja", "zh-Hant", "livemaker")
    )
    engine = project.config.engine.model_copy(update={"game_path": "game"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    picture = project.root / "game" / "parts" / name
    picture.parent.mkdir(parents=True)
    lettered = Image.new("RGBA", (80, 32), background)
    ImageDraw.Draw(lettered).rectangle((70, 0, 79, 31), fill=(200, 10, 10, 0))
    for left in (12, 26, 40):
        ImageDraw.Draw(lettered).rectangle((left, 9, left + 8, 19), fill=(240, 240, 240, 255))
    lettered.convert(mode).save(picture)
    region = {"box": [8, 6, 60, 22], "text": "スキル"}
    unit = TextUnit(
        id=images.unit_id(f"parts/{name}"), source="スキル", target=target, status=Status.MT,
        kind="image", engine_ref={"image": f"parts/{name}", "regions": [region]},
    )
    project = Project.load(project.root)
    save_image_units(project, StringFile(source_file="images", units=[unit]))
    return project


def _saved_in_editor(project: Project, picture: bytes, name: str = "skill.png") -> None:
    codec = PillowImageCodec()
    document = load_image_document(project, f"parts/{name}", codec)
    save_image_editor_document(
        project,
        document,
        picture,
        picture,
        load_image_text_layer(
            project, f"parts/{name}", source_fingerprint=document.source_fingerprint
        ),
        codec,
        animation=create_image_animation(f"parts/{name}", picture, codec),
    )


def _through_a_canvas(project: Project, name: str) -> bytes:
    """What the editor was given, as a browser canvas hands it back: alpha premultiplied."""
    given = load_image_document(project, f"parts/{name}", PillowImageCodec()).editor_image_bytes
    picture = Image.open(io.BytesIO(given)).convert("RGBA")
    picture.putdata(
        [
            tuple(min(255, round(round(c * a / 255) * 255 / a)) for c in (r, g, b)) + (a,)
            if a
            else (0, 0, 0, 0)
            for r, g, b, a in picture.getdata()
        ]
    )
    stream = io.BytesIO()
    picture.save(stream, "PNG")
    return stream.getvalue()


def _render(project: Project, font: Path) -> list[tuple[str, str]]:
    run = render_images(project, str(font), lambda rgb, mask: rgb, codec=PillowImageCodec())
    assert not run.errors
    return [(result.status, result.reason) for result in run.results]


@pytest.mark.parametrize("target", ["技能", "スキル"])
def test_a_picture_painted_in_the_editor_is_left_to_it(
    tmp_path: Path, make_font, target: str
) -> None:
    project = _skill_picture(tmp_path, target)
    font = make_font(tmp_path / "face.ttf", "Face", "技能スキル")
    _saved_in_editor(project, _png((255, 0, 0, 255)))
    draft = project.root / "out" / "parts" / "skill.png"
    before = draft.read_bytes()

    assert _render(project, font) == [("skipped", "edited by hand in the image editor")]
    assert draft.read_bytes() == before


def test_an_editor_save_that_painted_nothing_still_renders(tmp_path: Path, make_font) -> None:
    project = _skill_picture(tmp_path, "技能")
    font = make_font(tmp_path / "face.ttf", "Face", "技能スキル")
    assert _render(project, font) == [("rendered", "")]
    draft = project.root / "out" / "parts" / "skill.png"
    _saved_in_editor(project, draft.read_bytes())

    assert _render(project, font) == [("rendered", "")]


@pytest.mark.parametrize(
    ("name", "mode", "background"),
    [
        ("skill.png", "RGBA", (30, 30, 40, 128)),
        ("skill.png", "RGB", (30, 30, 40, 255)),
        ("skill.jpg", "RGB", (30, 30, 40, 255)),
    ],
)
def test_a_save_that_painted_nothing_renders_whatever_the_picture_is(
    tmp_path: Path, make_font, name: str, mode: str, background: tuple[int, int, int, int]
) -> None:
    project = _skill_picture(tmp_path, "技能", name, mode, background)
    font = make_font(tmp_path / "face.ttf", "Face", "技能スキル")
    assert _render(project, font) == [("rendered", "")]
    _saved_in_editor(project, _through_a_canvas(project, name), name)

    assert _render(project, font) == [("rendered", "")]


def test_an_editor_save_before_any_render_that_painted_nothing_still_renders(
    tmp_path: Path, make_font
) -> None:
    project = _skill_picture(tmp_path, "技能")
    font = make_font(tmp_path / "face.ttf", "Face", "技能スキル")
    _saved_in_editor(project, (project.root / "game" / "parts" / "skill.png").read_bytes())

    assert _render(project, font) == [("rendered", "")]


def test_a_second_editor_save_of_a_jpeg_that_painted_nothing_still_renders(
    tmp_path: Path, make_font
) -> None:
    project = _skill_picture(tmp_path, "技能", "skill.jpg", "RGB", (30, 30, 40, 255))
    font = make_font(tmp_path / "face.ttf", "Face", "技能スキル")
    _saved_in_editor(project, _through_a_canvas(project, "skill.jpg"), "skill.jpg")
    _saved_in_editor(project, _through_a_canvas(project, "skill.jpg"), "skill.jpg")

    assert _render(project, font) == [("rendered", "")]


def test_deleting_a_painted_draft_lets_render_draw_it_again(tmp_path: Path, make_font) -> None:
    project = _skill_picture(tmp_path, "技能")
    font = make_font(tmp_path / "face.ttf", "Face", "技能スキル")
    _saved_in_editor(project, _png((255, 0, 0, 255)))
    (project.root / "out" / "parts" / "skill.png").unlink()

    assert _render(project, font) == [("rendered", "")]


def test_a_game_update_lets_render_draw_a_painted_picture_again(
    tmp_path: Path, make_font
) -> None:
    project = _skill_picture(tmp_path, "技能")
    font = make_font(tmp_path / "face.ttf", "Face", "技能スキル")
    _saved_in_editor(project, _png((255, 0, 0, 255)))
    source = project.root / "game" / "parts" / "skill.png"
    updated = Image.open(source)
    updated.putpixel((0, 0), (40, 30, 30, 255))
    updated.save(source)

    assert _render(project, font) == [("rendered", "")]
