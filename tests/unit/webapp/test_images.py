"""RED/GREEN tests for the images domain bridge methods."""

from __future__ import annotations

import base64
import json
import logging
from io import BytesIO
from pathlib import Path

from PIL import Image

from bokucho import images as images_mod
from bokucho import render as render_mod
from bokucho.application.assets import extract_images, load_image_units, save_image_units
from bokucho.application.contracts import NullProgressSink
from bokucho.application.image_animation import create_image_animation
from bokucho.application.image_editing import load_image_document
from bokucho.application.image_editor_saving import save_image_editor_document
from bokucho.application.image_typesetting import ImageTextLayer
from bokucho.images import unit_id
from bokucho.infrastructure.pillow_image_codec import PillowImageCodec
from bokucho.model import Status, TextUnit
from bokucho.project import Project
from bokucho.webapp import api as api_module
from bokucho.webapp.api import Api
from bokucho.webapp.registry import JOBS


def _png_bytes(color: tuple[int, int, int, int], size: tuple[int, int] = (4, 3)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _seed_image_project(
    api: Api, tmp_path: Path, name: str, relative: str = "ui/face.png"
) -> tuple[Project, Path]:
    destination = tmp_path / name
    created = api.create_project(
        name=name,
        engine="livemaker",
        destination=str(destination),
        source_lang="ja",
        target_lang="zh-Hant",
    )
    assert "error" not in created
    project = Project.load(created["root"])
    source = project.root / "game" / Path(relative)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_png_bytes((255, 0, 0, 128)))
    engine = project.config.engine.model_copy(update={"game_path": "game"})
    project.save_config(project.config.model_copy(update={"engine": engine}))
    project = Project.load(project.root)
    assert "error" not in api.open_project(str(project.root))
    return project, source


def test_image_inventory_serves_persisted_entries(api: Api, tmp_path: Path) -> None:
    project, _source = _seed_image_project(api, tmp_path, "inventory")
    entry = {"image": "ui/face.png", "format": "PNG"}
    (project.root / "images-inventory.json").write_text(
        json.dumps([entry]), encoding="utf-8"
    )

    result = api.image_inventory()

    assert "error" not in result
    assert result["entries"] == [entry]


def test_an_ignored_image_is_listed_as_ignored_until_handed_back(
    api: Api, tmp_path: Path
) -> None:
    project, _source = _seed_image_project(api, tmp_path, "ignored")
    extract_images(project)

    assert api.set_images_ignored(["ui/face.png"], True) == {"changed": 1}
    assert api.image_inventory()["entries"][0]["ignored"] is True

    assert api.set_images_ignored(["ui/face.png"], False) == {"changed": 1}
    assert "ignored" not in api.image_inventory()["entries"][0]


def test_ignoring_a_path_outside_the_game_is_refused(api: Api, tmp_path: Path) -> None:
    _seed_image_project(api, tmp_path, "unsafe")

    assert api.set_images_ignored(["../face.png"], True)["error"]["kind"] == "validation"


def test_an_ocr_threshold_outside_zero_to_one_is_refused_and_not_saved(api: Api) -> None:
    before = api.state.settings.load()

    result = api.save_ocr_configuration(4, 1.5, 0.9)

    assert result["error"]["kind"] == "validation"
    assert api.state.settings.load() == before


def test_ocr_status_reports_the_pinned_model_cache(api: Api, monkeypatch) -> None:
    monkeypatch.setattr(
        api_module.images_mod,
        "ocr_model_cached",
        lambda selection: True,
    )
    monkeypatch.setattr(
        api_module.images_mod,
        "portable_ocr_runtime_cached",
        lambda: False,
    )

    result = api.ocr_status()

    assert result["detector"]["model_id"] == (
        "PaddlePaddle/PP-OCRv6_medium_det_safetensors"
    )
    assert result["recognizer"]["model_id"] == (
        "PaddlePaddle/PP-OCRv6_medium_rec_safetensors"
    )
    assert result["detection_threshold"] == 0.9
    assert result["recognition_threshold"] == 0.9
    assert result["runtime_cached"] is False
    assert result["runtime_download_bytes"] == 1_968_133_765
    assert result["detector"]["cached"] is True
    assert result["recognizer"]["cached"] is True
    for removed in ("selection", "models", "precisions", "ocr_prompt"):
        assert removed not in result


def test_image_data_serves_base64_png_bytes(api: Api, tmp_path: Path) -> None:
    _project, source = _seed_image_project(api, tmp_path, "data", relative="ui/button.png")

    result = api.image_data("ui/button.png")

    assert "error" not in result
    assert base64.b64decode(result["data"]) == source.read_bytes()


def test_image_data_rejects_a_path_traversal_attempt(api: Api, tmp_path: Path) -> None:
    _seed_image_project(api, tmp_path, "traversal")

    result = api.image_data("../project.toml")

    assert result["error"]["kind"] == "validation"


def test_image_document_round_trips_and_preserves_the_game_source(
    api: Api, tmp_path: Path
) -> None:
    project, source = _seed_image_project(api, tmp_path, "document", relative="ui/face.png")
    original_bytes = source.read_bytes()

    state = api.image_editor_state("ui/face.png")
    assert "error" not in state

    state["output_data"] = base64.b64encode(_png_bytes((0, 255, 0, 64))).decode("ascii")
    saved = api.save_image_editor_state("ui/face.png", state["source_fingerprint"], state)

    assert "error" not in saved
    output = project.root / "out" / "ui" / "face.png"
    assert output.is_file()
    with Image.open(output) as image:
        assert image.getpixel((0, 0)) == (0, 255, 0, 64)
    assert source.read_bytes() == original_bytes


def test_scanning_pictures_again_refuses_a_threshold_out_of_range(
    api: Api, tmp_path: Path, registry: dict
) -> None:
    project, _source = _seed_image_project(api, tmp_path, "threshold")
    extract_images(project)
    registry["images-ocr-selected"] = JOBS["images-ocr-selected"]

    result = api.start_job(
        "images-ocr-selected", {"images": ["ui/face.png"], "detection_threshold": 1.5}
    )

    assert result["error"]["kind"] == "validation"


def test_job_options_are_an_object_and_only_for_a_job_that_takes_them(
    api: Api, tmp_path: Path, registry: dict
) -> None:
    project, _source = _seed_image_project(api, tmp_path, "options")
    extract_images(project)
    registry["images-ocr-selected"] = JOBS["images-ocr-selected"]
    registry["images-extract"] = JOBS["images-extract"]

    assert api.start_job("images-ocr-selected", "ui/face.png")["error"]["kind"] == "validation"
    assert (
        api.start_job("images-extract", {"images": ["ui/face.png"]})["error"]["kind"]
        == "validation"
    )


class _Renderer:
    def __init__(self) -> None:
        self.rendered: list[str] = []

    def render(
        self, source: Path, manifest: dict, target: str, destination: Path, size=None
    ) -> dict:
        self.rendered.append(manifest["image"])
        return {"image": manifest["image"], "status": "rendered", "reason": ""}

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class _NeverCancelled:
    def add_cancel_callback(self, callback) -> None:
        pass

    def raise_if_cancelled(self) -> None:
        pass


def _saved_in_editor(project: Project, relative: str, picture: bytes) -> None:
    codec = PillowImageCodec()
    save_image_editor_document(
        project,
        load_image_document(project, relative, codec),
        picture,
        picture,
        ImageTextLayer(),
        codec,
        animation=create_image_animation(relative, picture, codec),
    )


def test_the_render_job_leaves_a_picture_painted_by_hand_alone(
    api: Api, tmp_path: Path, monkeypatch, caplog
) -> None:
    project, _source = _seed_image_project(api, tmp_path, "render")
    pictures = ("ui/boxes.png", "ui/face.png", "ui/hand.png")
    for relative in pictures:
        (project.root / "game" / relative).write_bytes(_png_bytes((0, 0, 0, 255)))
    region = {"box": [0, 0, 4, 3], "text": "顔"}
    units = load_image_units(project)
    units.units = [
        TextUnit(
            id=unit_id(relative), source="顔", target="臉", status=Status.MT, kind="image",
            engine_ref={"image": relative, "format": "PNG", "regions": [region]},
        )
        for relative in pictures
    ]
    save_image_units(project, units)
    _saved_in_editor(project, "ui/boxes.png", _png_bytes((0, 0, 0, 255)))
    _saved_in_editor(project, "ui/hand.png", _png_bytes((255, 0, 0, 255)))
    renderer = _Renderer()
    monkeypatch.setattr(render_mod, "resolve_font", lambda configured, root: "face.ttf")
    monkeypatch.setattr(images_mod, "eraser_model_path", lambda: Path(__file__))
    monkeypatch.setattr(images_mod, "ensure_portable_ocr_runtime", lambda **kwargs: None)
    monkeypatch.setattr(images_mod, "get_renderer", lambda model, font: renderer)
    api.state.reload()

    with caplog.at_level(logging.INFO, logger="bokucho"):
        result = JOBS["images-render"](api.state)(NullProgressSink(), _NeverCancelled())

    assert renderer.rendered == ["ui/boxes.png", "ui/face.png"]
    assert (result["rendered"], result["skipped"]) == (2, 1)
    assert "ui/hand.png" in caplog.text and "painted by hand" in caplog.text


class _SizingRenderer(_Renderer):
    """Letters each picture at the size its translation asks for, unless one is set."""

    def __init__(self, sizes: dict[str, int]) -> None:
        super().__init__()
        self.sizes = sizes
        self.calls: list[tuple[str, int | None]] = []

    def render(
        self, source: Path, manifest: dict, target: str, destination: Path, size=None
    ) -> dict:
        image = manifest["image"]
        self.calls.append((image, size))
        if image not in self.sizes:
            raise ValueError("the picture cannot be read")
        chosen = size or self.sizes[image]
        return {"image": image, "status": "rendered", "reason": "", "size": chosen}


def _render_job(
    api: Api, tmp_path: Path, monkeypatch, name: str, pictures: tuple[str, ...], renderer
) -> dict:
    project, _source = _seed_image_project(api, tmp_path, name)
    for relative in pictures:
        (project.root / "game" / relative).write_bytes(_png_bytes((0, 0, 0, 255)))
    units = load_image_units(project)
    units.units = [
        TextUnit(
            id=unit_id(relative), source="顔", target="臉", status=Status.MT, kind="image",
            engine_ref={"image": relative, "format": "PNG", "regions": [{"box": [0, 0, 4, 3]}]},
        )
        for relative in pictures
    ]
    save_image_units(project, units)
    monkeypatch.setattr(render_mod, "resolve_font", lambda configured, root: "face.ttf")
    monkeypatch.setattr(images_mod, "eraser_model_path", lambda: Path(__file__))
    monkeypatch.setattr(images_mod, "ensure_portable_ocr_runtime", lambda **kwargs: None)
    monkeypatch.setattr(images_mod, "get_renderer", lambda model, font: renderer)
    api.state.reload()
    return JOBS["images-render"](api.state)(NullProgressSink(), _NeverCancelled())


def test_one_picture_that_cannot_be_rendered_does_not_end_the_run(
    api: Api, tmp_path: Path, monkeypatch, caplog
) -> None:
    renderer = _SizingRenderer({"ui/face.png": 14})

    with caplog.at_level(logging.WARNING, logger="bokucho"):
        result = _render_job(
            api, tmp_path, monkeypatch, "bad", ("ui/bad.png", "ui/face.png"), renderer
        )

    assert (result["rendered"], result["failed"]) == (1, 1)
    assert "ui/bad.png" in caplog.text and "cannot be read" in caplog.text


def test_the_render_job_letters_a_set_of_pictures_at_one_size(
    api: Api, tmp_path: Path, monkeypatch
) -> None:
    sizes = {"ui/a.png": 13, "ui/b.png": 15, "ui/c.png": 14}
    renderer = _SizingRenderer(sizes)

    result = _render_job(api, tmp_path, monkeypatch, "set", tuple(sizes), renderer)

    assert renderer.calls[3:] == [("ui/a.png", 14), ("ui/b.png", 14)]
    assert result["rendered"] == 3


def test_an_editor_state_outside_its_timeline_is_refused_before_anything_is_written(
    api: Api, tmp_path: Path
) -> None:
    project, _source = _seed_image_project(api, tmp_path, "timeline")
    state = api.image_editor_state("ui/face.png")
    state["active_frame"] = len(state["frames"])
    state["output_data"] = state["frames"][0]["layers"][0]["data"]

    result = api.save_image_editor_state("ui/face.png", state["source_fingerprint"], state)

    assert result["error"]["kind"] == "invalid_project"
    assert "active frame" in result["error"]["message"]
    assert not (project.root / "out" / "ui" / "face.png").exists()


def test_a_text_region_with_no_area_is_refused_before_anything_is_written(
    api: Api, tmp_path: Path
) -> None:
    project, _source = _seed_image_project(api, tmp_path, "region")
    state = api.image_editor_state("ui/face.png")
    state["text_layer"]["region"] = [2, 0, 2, 3]
    state["output_data"] = state["frames"][0]["layers"][0]["data"]

    result = api.save_image_editor_state("ui/face.png", state["source_fingerprint"], state)

    assert result["error"]["kind"] == "validation"
    assert "Text layer region is invalid" in result["error"]["message"]
    assert not (project.root / "out" / "ui" / "face.png").exists()
