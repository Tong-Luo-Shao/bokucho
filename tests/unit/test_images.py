"""Tests for Phase 7a: image inventory, OCR units, drafts."""

import io
import json
import os
import sys
from pathlib import Path
from threading import Event, Thread
from types import ModuleType

import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from bokucho import cli
from bokucho import render as render_mod
from bokucho.application.errors import JobCancelled, MissingDependency
from bokucho.infrastructure.ocr_runtime import OCR_RUNTIME_FILES
from bokucho.model import Status
from bokucho.project import Project

runner = CliRunner()

_ENCRYPTED_KEY = "a24255501e9aea7de2d81f1b2d070d73"


def _encrypted_game(tmp_path):
    """A game tree with one encrypted picture, key alongside in data/."""
    from bokucho.engines.rpgmv_assets import encrypt

    game = tmp_path / "game"
    (game / "data").mkdir(parents=True)
    (game / "data" / "System.json").write_text(
        json.dumps({"hasEncryptedImages": True, "encryptionKey": _ENCRYPTED_KEY}),
        encoding="utf-8",
    )
    pictures = game / "img" / "pictures"
    pictures.mkdir(parents=True)
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(buffer, format="PNG")
    asset = pictures / "title.rpgmvp"
    asset.write_bytes(encrypt(buffer.getvalue(), _ENCRYPTED_KEY))
    return game, asset


def _project_with_images(tmp_path) -> Project:
    proj_dir = tmp_path / "proj"
    assert runner.invoke(cli.app, ["init", str(proj_dir), "--name", "t"]).exit_code == 0
    game = proj_dir / "game"
    (game / "ui").mkdir(parents=True)
    button = Image.new("RGBA", (80, 20), (255, 0, 0, 128))
    ImageDraw.Draw(button).rectangle((20, 6, 55, 14), fill=(20, 20, 20, 255))
    button.save(game / "ui" / "button.png")
    Image.new("RGB", (32, 32), "white").save(game / "bg.png")
    toml = proj_dir / "project.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace('game_path = ""', 'game_path = "game"'),
        encoding="utf-8",
    )
    return Project.load(proj_dir)


def test_extract_inventories_images(tmp_path):
    project = _project_with_images(tmp_path)
    result = runner.invoke(cli.app, ["images", "extract", str(project.root)])
    assert result.exit_code == 0, result.stdout

    inventory = json.loads((project.root / "images-inventory.json").read_text(encoding="utf-8"))
    by_name = {e["image"]: e for e in inventory}
    assert by_name["ui/button.png"]["mode"] == "RGBA"
    assert by_name["bg.png"]["width"] == 32


def test_load_inventory_round_trips_extract(tmp_path):
    from bokucho.application.assets import load_inventory

    project = _project_with_images(tmp_path)
    runner.invoke(cli.app, ["images", "extract", str(project.root)])
    entries = load_inventory(project)
    assert {e["image"] for e in entries} == {"ui/button.png", "bg.png"}


def test_load_inventory_missing_is_empty(tmp_path):
    from bokucho.application.assets import load_inventory

    project = _project_with_images(tmp_path)
    assert load_inventory(project) == ()


def test_iter_images_finds_an_encrypted_asset(tmp_path):
    from bokucho import images as image_helpers

    game, asset = _encrypted_game(tmp_path)
    assert image_helpers.iter_images(game) == [asset]


def test_inspect_reports_the_real_size_of_an_encrypted_asset(tmp_path):
    from bokucho import images as image_helpers

    _, asset = _encrypted_game(tmp_path)
    meta = image_helpers.inspect(asset)
    assert (meta["format"], meta["width"], meta["height"]) == ("PNG", 32, 24)


def test_to_png_converts_an_encrypted_asset(tmp_path):
    from bokucho import images as image_helpers

    _, asset = _encrypted_game(tmp_path)
    out = tmp_path / "converted.png"
    image_helpers.to_png(asset, out)
    with Image.open(out) as img:
        assert img.size == (32, 24)


def test_extract_ocr_creates_units_once(tmp_path, monkeypatch):
    project = _project_with_images(tmp_path)

    class _Detector:
        def detect_many(self, paths):
            return [[[0, 0, 1000, 1000]] for _ in paths]

    class _Reader:
        batch_size = 4

        def read_located(self, paths, boxes):
            return [
                json.dumps(
                    [{"bbox_2d": box, "text": "ボタン", "score": 1.0} for box in located]
                )
                for located in boxes
            ]

    def _get_ocr(*, allow_download, locating=False, reads_crops=False):
        assert allow_download
        assert locating != reads_crops
        return _Detector() if locating else _Reader()

    monkeypatch.setattr(cli.images_mod, "get_ocr", _get_ocr)

    assert runner.invoke(cli.app, ["images", "extract", str(project.root), "--ocr"]).exit_code == 0
    assert runner.invoke(cli.app, ["images", "extract", str(project.root), "--ocr"]).exit_code == 0

    units = [u for sf in project.load_string_files() for u in sf.units]
    assert len(units) == 2
    assert all(u.kind == "image" and u.source == "ボタン" for u in units)


def _complete_ocr_snapshot(path: Path) -> Path:
    from bokucho import images as image_helpers

    path.mkdir(parents=True)
    for name in (*image_helpers.OCR_MODEL_FILES, *image_helpers.OCR_MODEL_WEIGHTS):
        (path / name).touch()
    return path


def test_ocr_prefers_the_portable_model_without_contacting_the_hub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    snapshot = _complete_ocr_snapshot(
        tmp_path / "models" / image_helpers.OCR_MODEL_DIRNAME
    )
    hub = ModuleType("huggingface_hub")

    def unexpected_download(*args, **kwargs):
        pytest.fail("portable OCR must not contact the Hugging Face Hub")

    hub.snapshot_download = unexpected_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(
        image_helpers,
        "portable_ocr_model_dir",
        lambda selection: snapshot,
    )

    assert image_helpers.ocr_model_snapshot(allow_download=False) == snapshot
    assert image_helpers.ocr_model_cached() is True


def test_release_ocr_resolution_ignores_an_adjacent_model_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    adjacent = _complete_ocr_snapshot(tmp_path / "adjacent")
    pinned = _complete_ocr_snapshot(tmp_path / "pinned")
    calls: list[dict[str, object]] = []
    hub = ModuleType("huggingface_hub")

    def download(model_id: str, **kwargs) -> str:
        calls.append({"model_id": model_id, **kwargs})
        return str(pinned)

    hub.snapshot_download = download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(image_helpers, "portable_ocr_model_dir", lambda: adjacent)

    resolved = image_helpers.ocr_model_snapshot(
        allow_download=True,
        allow_portable=False,
    )

    assert resolved == pinned
    assert calls == [
        {
            "model_id": image_helpers.OCR_MODEL_ID,
            "revision": image_helpers.OCR_MODEL_REVISION,
            "local_files_only": False,
        }
    ]


def test_ocr_snapshot_failure_is_a_presentable_application_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    hub = ModuleType("huggingface_hub")

    def failed_download(*args, **kwargs):
        raise OSError("network unavailable")

    hub.snapshot_download = failed_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(
        image_helpers,
        "portable_ocr_model_dir",
        lambda selection: tmp_path / "missing-model",
    )

    with pytest.raises(MissingDependency, match="OCR model"):
        image_helpers.ocr_model_snapshot(allow_download=True)


def test_ocr_snapshot_reports_pinned_manifest_download_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    snapshot = _complete_ocr_snapshot(tmp_path / "snapshot")
    hub = ModuleType("huggingface_hub")
    calls: list[str] = []
    weight_name = next(
        name
        for name, _size in image_helpers.OCR_MODEL_DOWNLOAD_MANIFEST
        if name in image_helpers.OCR_MODEL_WEIGHTS
    )

    def download(model_id: str, filename: str, **kwargs) -> str:
        assert model_id == image_helpers.OCR_MODEL_ID
        calls.append(filename)
        if filename == weight_name:
            size = dict(image_helpers.OCR_MODEL_DOWNLOAD_MANIFEST)[filename]
            with kwargs["tqdm_class"](total=size, initial=0) as progress:
                progress.update(size // 2)
                progress.update(size - size // 2)
        return str(snapshot / filename)

    hub.hf_hub_download = download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    progress_events: list[tuple[int, int]] = []

    resolved = image_helpers.ocr_model_snapshot(
        allow_download=True,
        allow_portable=False,
        download_progress=lambda current, total: progress_events.append(
            (current, total)
        ),
    )

    weight_index = next(
        index
        for index, (name, _size) in enumerate(
            image_helpers.OCR_MODEL_DOWNLOAD_MANIFEST
        )
        if name == weight_name
    )
    bytes_before_weight = sum(
        size
        for _name, size in image_helpers.OCR_MODEL_DOWNLOAD_MANIFEST[:weight_index]
    )
    halfway = bytes_before_weight + (
        dict(image_helpers.OCR_MODEL_DOWNLOAD_MANIFEST)[weight_name] // 2
    )
    assert resolved == snapshot
    assert calls == [name for name, _size in image_helpers.OCR_MODEL_DOWNLOAD_MANIFEST]
    from bokucho.infrastructure.pp_ocr import RECOGNIZER_CHECKPOINT

    assert (halfway, RECOGNIZER_CHECKPOINT.download_bytes) in progress_events
    assert progress_events[-1] == (
        RECOGNIZER_CHECKPOINT.download_bytes,
        RECOGNIZER_CHECKPOINT.download_bytes,
    )
    assert len(progress_events) <= 101


def test_ocr_download_progress_preserves_job_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    hub = ModuleType("huggingface_hub")
    hub.hf_hub_download = lambda *args, **kwargs: str(tmp_path / "unused")
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    def cancel(current: int, total: int) -> None:
        raise JobCancelled()

    with pytest.raises(JobCancelled):
        image_helpers.ocr_model_snapshot(
            allow_download=True,
            allow_portable=False,
            download_progress=cancel,
        )


def test_ocr_initialization_failure_is_a_presentable_application_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    snapshot = _complete_ocr_snapshot(tmp_path / "snapshot")
    def broken_reader(model_path: Path, **options: object) -> object:
        raise RuntimeError(f"cannot load {model_path}")

    monkeypatch.setattr(image_helpers, "_pp_ocr_reader", broken_reader)
    monkeypatch.setattr(
        image_helpers,
        "ocr_model_snapshot",
        lambda **_: snapshot,
    )

    with pytest.raises(MissingDependency, match="OCR runtime"):
        image_helpers.get_ocr(allow_download=False, reads_crops=True)


def test_missing_in_process_runtime_fails_before_model_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    monkeypatch.setattr(image_helpers, "portable_ocr_runtime", lambda: None)
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "transformers", None)
    monkeypatch.setattr(
        image_helpers,
        "ocr_model_snapshot",
        lambda **_: pytest.fail("model download started"),
    )

    with pytest.raises(MissingDependency, match="OCR runtime"):
        image_helpers.get_ocr(allow_download=True, reads_crops=True)


class _FakeSidecarInput:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.lines.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeSidecarOutput:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.closed = False

    def readline(self) -> str:
        return next(self.responses, "")

    def close(self) -> None:
        self.closed = True


class _FakeSidecarProcess:
    def __init__(self, responses: list[str]) -> None:
        self.stdin = _FakeSidecarInput()
        self.stdout = _FakeSidecarOutput(responses)
        self.returncode: int | None = None
        self.waited = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class _BlockingSidecarOutput:
    def __init__(self, terminated: Event) -> None:
        self._terminated = terminated
        self.reading = Event()
        self.closed = False

    def readline(self) -> str:
        self.reading.set()
        self._terminated.wait(timeout=2)
        return ""

    def close(self) -> None:
        self.closed = True


class _BlockingSidecarProcess(_FakeSidecarProcess):
    def __init__(self) -> None:
        self.terminated = Event()
        super().__init__([])
        self.stdout = _BlockingSidecarOutput(self.terminated)

    def terminate(self) -> None:
        self.returncode = 1
        self.terminated.set()


def test_portable_ocr_cancel_interrupts_a_blocked_sidecar_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    process = _BlockingSidecarProcess()
    monkeypatch.setattr(image_helpers.subprocess, "Popen", lambda *args, **kwargs: process)
    reader = image_helpers.PortableOcrReader(
        tmp_path / "python.exe",
        tmp_path / "ocr_runner.py",
        tmp_path / "model",
    )
    failures: list[BaseException] = []

    def recognize() -> None:
        try:
            reader(tmp_path / "image.png")
        except BaseException as error:
            failures.append(error)

    worker = Thread(target=recognize)
    worker.start()
    assert process.stdout.reading.wait(timeout=2)

    reader.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], JobCancelled)
    assert process.terminated.is_set()


def test_portable_runtime_and_smoke_sample_resolve_beside_the_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    python = tmp_path / "ocr-runtime" / "python" / "python.exe"
    runner_script = tmp_path / "ocr-runtime" / "ocr_runner.py"
    packages = tmp_path / "ocr-runtime" / "python" / "Lib" / "site-packages"
    packages.mkdir(parents=True)
    python.touch()
    for name in OCR_RUNTIME_FILES:
        (tmp_path / "ocr-runtime" / name).touch()
    for name in ("torch", "torchvision", "cv2"):
        (packages / name).mkdir()
    (tmp_path / "ocr-runtime" / "runtime.json").write_text(
        json.dumps(
            {
                "torch": "2.13.0+cu130",
                "cuda": "13.0",
                "transformers": image_helpers.OCR_TRANSFORMERS_VERSION,
                "torch_backend": image_helpers.OCR_TORCH_BACKEND,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(image_helpers, "_portable_root_dir", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "transformers", None)

    assert image_helpers.portable_ocr_runtime() == (python, runner_script)
    (packages / "torch").rmdir()
    assert image_helpers.portable_ocr_runtime() is None


def test_portable_ocr_reader_reuses_one_json_lines_process_and_closes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    process = _FakeSidecarProcess(
        [
            '{"ok": true, "text": "一"}\n',
            '{"ok": true, "text": "二"}\n',
        ]
    )
    launches: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_popen(*args, **kwargs):
        launches.append((args, kwargs))
        return process

    monkeypatch.setattr(image_helpers.subprocess, "Popen", fake_popen)
    model = tmp_path / "models" / image_helpers.OCR_MODEL_DIRNAME
    reader = image_helpers.PortableOcrReader(
        tmp_path / "ocr-runtime" / "python" / "python.exe",
        tmp_path / "ocr-runtime" / "ocr_runner.py",
        model,
    )

    assert reader(tmp_path / "first.png") == "一"
    assert reader(tmp_path / "second.png") == "二"
    reader.close()

    assert len(launches) == 1
    command = launches[0][0][0]
    assert command[1:3] == ["-I", str(tmp_path / "ocr-runtime" / "ocr_runner.py")]
    assert command[3:] == [
        "--model",
        str(model),
        "--batch-size",
        "8",
        "--detection-threshold",
        "0.9",
        "--recognition-threshold",
        "0.9",
    ]
    assert [json.loads(line)["path"] for line in process.stdin.lines] == [
        str((tmp_path / "first.png").resolve()),
        str((tmp_path / "second.png").resolve()),
    ]
    assert launches[0][1]["env"]["HF_HUB_OFFLINE"] == "1"
    assert launches[0][1]["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert process.stdin.closed is True
    assert process.stdout.closed is True
    assert process.waited is True


def test_a_picture_the_render_sidecar_refuses_does_not_stop_the_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bokucho import images as image_helpers

    process = _FakeSidecarProcess(
        [
            '{"ok": false, "error": "cannot identify image file"}\n',
            '{"ok": true, "image": "ui/good.png", "status": "rendered", "reason": ""}\n',
        ]
    )
    monkeypatch.setattr(image_helpers.subprocess, "Popen", lambda *args, **kwargs: process)
    renderer = image_helpers.PortableRenderer(
        tmp_path / "python.exe", tmp_path / "render_runner.py", tmp_path / "lama.pt", "face.ttf"
    )

    with pytest.raises(ValueError, match="cannot identify image file"):
        renderer.render(tmp_path / "bad.png", {"image": "ui/bad.png"}, "壞", tmp_path / "bad.png")
    result = renderer.render(
        tmp_path / "good.png", {"image": "ui/good.png"}, "好", tmp_path / "good.png"
    )
    renderer.close()

    assert result["status"] == "rendered"


def test_qa_does_not_check_engine_encoding_for_image_units(tmp_path):
    from bokucho.model import TextUnit
    from bokucho.qa import check_unit

    unit = TextUnit(id="img:x", source="ボタン", target="您好貓", kind="image",
                    status=Status.MT)
    assert check_unit(unit) == []


GAL_DIR = os.environ.get("BOKUCHO_TEST_GAL")


@pytest.mark.skipif(not GAL_DIR, reason="set BOKUCHO_TEST_GAL to a dir with .gal samples")
def test_real_gal_to_png_round_trip(tmp_path):
    from bokucho import images as images_mod

    gals = [p for p in Path(GAL_DIR).rglob("*.gal")]
    assert gals, "no .gal files in BOKUCHO_TEST_GAL"
    for gal in gals:
        meta = images_mod.inspect(gal)
        png = tmp_path / (gal.stem + ".png")
        images_mod.to_png(gal, png)
        out = images_mod.inspect(png)
        assert (out["width"], out["height"], out["mode"]) == (
            meta["width"], meta["height"], meta["mode"],
        )


def _fake_inpaint(rgb, mask):
    out = rgb.copy()
    out[mask] = (210, 205, 195)
    return out


def _ascii_font():
    from pathlib import Path as _P
    for cand in (r"C:\Windows\Fonts\arial.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if _P(cand).is_file():
            return cand
    import pytest as _pytest
    _pytest.skip("no test font available")


def test_images_render_missing_model_exits_1(tmp_path, monkeypatch):
    project = _project_with_images(tmp_path)
    runner.invoke(cli.app, ["images", "extract", str(project.root)])

    toml = project.root / "project.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + f'\n[images]\nfont = "{_ascii_font()}"\n'
        .replace("\\", "\\\\"),
        encoding="utf-8",
    )

    def _raise():
        raise render_mod.ModelMissingError("LaMa model not found: x\ncurl ...")

    monkeypatch.setattr(cli.render_mod, "resolve_model_path", _raise)
    result = runner.invoke(cli.app, ["images", "render", str(project.root)])
    assert result.exit_code == 1
    assert "LaMa model not found" in result.output


def test_images_render_isolates_bad_order(tmp_path, monkeypatch):
    project = _project_with_images(tmp_path)
    runner.invoke(cli.app, ["images", "extract", str(project.root)])

    [sf] = [s for s in project.load_string_files() if s.by_id("img:ui/button.png")]
    unit = sf.by_id("img:ui/button.png")
    unit.target = "OK"
    unit.engine_ref["typesetting"] = {"region": [5, 2, 75, 18]}
    project.save_string_file(sf, "images.json")

    toml = project.root / "project.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + f'\n[images]\nfont = "{_ascii_font()}"\n'
        .replace("\\", "\\\\"),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.render_mod, "resolve_model_path", lambda: Path("fake.pt"))
    monkeypatch.setattr(cli.render_mod, "make_inpaint_fn", lambda p: _fake_inpaint)

    [sf] = [s for s in project.load_string_files() if s.by_id("img:bg.png")]
    broken = sf.by_id("img:bg.png")
    broken.target = "X"
    broken.engine_ref["typesetting"] = {"region": [1, 1, 20, 10]}
    project.save_string_file(sf, "images.json")
    (project.root / "game" / "bg.png").write_bytes(b"not a picture")

    result = runner.invoke(cli.app, ["images", "render", str(project.root)])
    assert result.exit_code == 0, result.stdout
    assert "Rendered 1 draft(s)" in result.stdout
    assert "failed: bg.png" in result.output
    assert (project.root / "out" / "ui" / "button.png").is_file()


def _lettered(tmp_path: Path, name: str, ink: int, under: tuple[int, int, int]) -> Path:
    """A texture showing one stroke of ``ink`` on nothing, saved over ``under``."""
    picture = Image.new("RGBA", (16, 8), (*under, 0))
    for x in range(4, 12):
        picture.putpixel((x, 4), (ink, ink, ink, 255))
    path = tmp_path / name
    picture.save(path)
    return path


def test_a_picture_is_composited_onto_the_shade_its_lettering_shows_against(
    tmp_path: Path,
) -> None:
    from bokucho import images as image_helpers

    dark = image_helpers.opaque_view(
        _lettered(tmp_path, "dark.png", 20, (0, 0, 0)), tmp_path / "dark-flat.png"
    )
    bright = image_helpers.opaque_view(
        _lettered(tmp_path, "bright.png", 240, (0, 0, 0)), tmp_path / "bright-flat.png"
    )

    assert Image.open(dark).getpixel((0, 0)) == (255, 255, 255)
    assert Image.open(bright).getpixel((0, 0)) == (0, 0, 0)


def test_an_opaque_picture_is_handed_over_as_it_is(tmp_path: Path) -> None:
    from bokucho import images as image_helpers

    source = tmp_path / "opaque.png"
    Image.new("RGB", (8, 8), (30, 40, 50)).save(source)
    destination = tmp_path / "opaque-flat.png"

    assert image_helpers.opaque_view(source, destination) == source
    assert not destination.exists()


def test_the_colour_stored_under_transparency_does_not_reach_the_model(
    tmp_path: Path,
) -> None:
    from bokucho import images as image_helpers

    source = _lettered(tmp_path, "bled.png", 20, (200, 30, 30))

    flat = Image.open(image_helpers.opaque_view(source, tmp_path / "bled-flat.png"))

    assert flat.getpixel((0, 0)) == (255, 255, 255)
    assert flat.getpixel((4, 4)) == (20, 20, 20)


def test_a_converted_picture_is_flattened_onto_its_own_name(tmp_path: Path) -> None:
    from bokucho import images as image_helpers

    scratch = _lettered(tmp_path, "converted.png", 20, (0, 0, 0))

    assert image_helpers.opaque_view(scratch, scratch) == scratch
    assert Image.open(scratch).convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_a_checkout_without_the_staged_runtime_renders_in_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("torch")
    from bokucho import images as image_helpers

    monkeypatch.setattr(image_helpers, "portable_renderer", lambda: None)

    renderer = image_helpers.get_renderer(tmp_path / "big-lama.pt", "Arial")

    assert isinstance(renderer, image_helpers.InProcessRenderer)


def test_rendering_without_torch_still_asks_for_the_portable_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from bokucho import images as image_helpers

    monkeypatch.setattr(image_helpers, "portable_renderer", lambda: None)
    monkeypatch.setitem(sys.modules, "torch", None)

    with pytest.raises(MissingDependency, match="render runtime"):
        image_helpers.get_renderer(tmp_path / "big-lama.pt", "Arial")


def test_the_in_process_renderer_loads_the_model_only_once_a_picture_needs_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("torch")
    from bokucho import images as image_helpers
    from bokucho import render as render_mod

    loads: list[Path] = []

    def make_inpaint_fn(model_path: Path):
        loads.append(model_path)
        return lambda rgb, mask: rgb

    monkeypatch.setattr(render_mod, "make_inpaint_fn", make_inpaint_fn)
    monkeypatch.setattr(image_helpers, "portable_renderer", lambda: None)

    renderer = image_helpers.get_renderer(tmp_path / "big-lama.pt", "Arial")
    assert loads == [], "the model was loaded before any picture asked for it"

    rendered: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        render_mod,
        "render_image",
        lambda source, manifest, target, font, inpaint, destination, size: rendered.append(
            (source, target)
        )
        or render_mod.RenderResult(image="a.png", status="rendered", reason=""),
    )

    result = renderer.render(tmp_path / "a.png", {}, "譯文", tmp_path / "out.png")

    assert loads == [tmp_path / "big-lama.pt"]
    assert rendered == [(tmp_path / "a.png", "譯文")]
    assert result == {"image": "a.png", "status": "rendered", "reason": "", "size": None}
