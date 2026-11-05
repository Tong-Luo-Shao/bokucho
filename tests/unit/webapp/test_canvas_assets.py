"""RED/GREEN tests for the canvas image editor module (batch 6)."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image

from bokucho.project import Project
from bokucho.webapp.api import Api


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


def test_text_layer_round_trips_with_style_fields_mirroring_the_qt_panel(
    api: Api, tmp_path: Path
) -> None:
    _project, _source = _seed_image_project(api, tmp_path, "canvas-text-layer")

    state = api.image_editor_state("ui/face.png")
    assert "error" not in state
    assert set(state["text_layer"]["style"]) == {
        "font_family",
        "font_size",
        "fill",
        "stroke",
        "stroke_width",
        "background",
        "alignment",
    }

    state["text_layer"]["target"] = "改稿"
    state["text_layer"]["region"] = [0, 0, 4, 3]
    state["text_layer"]["style"]["font_size"] = 32
    state["output_data"] = base64.b64encode(_png_bytes((0, 0, 0, 255))).decode("ascii")
    result = api.save_image_editor_state("ui/face.png", state["source_fingerprint"], state)
    assert "error" not in result

    reloaded = api.image_editor_state("ui/face.png")["text_layer"]
    assert reloaded["target"] == "改稿"
    assert reloaded["region"] == [0, 0, 4, 3]
    assert reloaded["style"]["font_size"] == 32
