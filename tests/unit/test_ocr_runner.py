from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest


def _serve():
    runner_path = Path(__file__).resolve().parents[2] / "tools" / "ocr_runner.py"
    spec = importlib.util.spec_from_file_location("bokucho_ocr_runner", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.serve


def test_runner_initializes_once_and_returns_one_response_per_request(
    tmp_path: Path,
) -> None:
    requests = io.StringIO(
        json.dumps({"path": str(tmp_path / "one.png")})
        + "\n"
        + json.dumps({"path": str(tmp_path / "two.png")})
        + "\n"
    )
    responses = io.StringIO()
    initialized: list[tuple[Path, dict[str, object]]] = []

    def reader_factory(model_path: Path, **options: object):
        print("library initialization noise")
        initialized.append((model_path, options))

        def reader(image_path: str) -> str:
            print("library inference noise")
            return Path(image_path).stem

        return reader

    with redirect_stdout(responses):
        _serve()(tmp_path / "model", requests, responses, reader_factory=reader_factory)

    payloads = [json.loads(line) for line in responses.getvalue().splitlines()]
    assert initialized == [(tmp_path / "model", {"batch_size": 8})]
    assert payloads == [
        {"ok": True, "text": "one"},
        {"ok": True, "text": "two"},
    ]


def test_runner_recognizes_a_batch_in_one_reader_call(tmp_path: Path) -> None:
    paths = [str(tmp_path / f"{index}.png") for index in range(4)]
    requests = io.StringIO(json.dumps({"paths": paths}) + "\n")
    responses = io.StringIO()
    batches: list[list[str]] = []

    class Reader:
        def __call__(self, image_path: str) -> str:
            raise AssertionError(f"batch unexpectedly split at {image_path}")

        def read_many(self, image_paths: list[str]) -> list[str]:
            batches.append(image_paths)
            return [Path(path).stem for path in image_paths]

    _serve()(
        tmp_path / "model",
        requests,
        responses,
        reader_factory=lambda model_path, **options: Reader(),
    )

    assert batches == [paths]
    assert json.loads(responses.getvalue()) == {
        "ok": True,
        "texts": ["0", "1", "2", "3"],
    }


def test_runner_reports_the_loaded_runtime_without_recognizing_an_image(
    tmp_path: Path,
) -> None:
    requests = io.StringIO(json.dumps({"action": "status"}) + "\n")
    responses = io.StringIO()
    report = {
        "requested_precision": "INT4",
        "weight_format": "NF4",
        "compute_dtype": "BF16",
        "quantized_linear_layers": 224,
        "model_memory_bytes": 3_100_000_000,
    }

    class Reader:
        runtime_report = report

        def __call__(self, image_path: str) -> str:
            raise AssertionError(f"status request recognized {image_path}")

    _serve()(
        tmp_path / "model",
        requests,
        responses,
        reader_factory=lambda model_path, **options: Reader(),
    )

    assert json.loads(responses.getvalue()) == {"ok": True, "runtime": report}


def test_runner_keeps_protocol_valid_when_recognition_fails(tmp_path: Path) -> None:
    requests = io.StringIO(json.dumps({"path": str(tmp_path / "bad.png")}) + "\n")
    responses = io.StringIO()

    def reader_factory(model_path: Path, **options: object):
        def fail(image_path: str) -> str:
            raise RuntimeError("bad image")

        return fail

    _serve()(tmp_path / "model", requests, responses, reader_factory=reader_factory)

    payload = json.loads(responses.getvalue())
    assert payload["ok"] is False
    assert payload["error"] == "bad image"


def test_runner_response_is_safe_when_windows_inherits_a_legacy_encoding(
    tmp_path: Path,
) -> None:
    requests = io.StringIO(json.dumps({"path": str(tmp_path / "image.png")}) + "\n")
    buffer = io.BytesIO()
    responses = io.TextIOWrapper(buffer, encoding="cp950")

    _serve()(
        tmp_path / "model",
        requests,
        responses,
        reader_factory=lambda model_path, **options: (lambda image_path: "日本語"),
    )
    responses.flush()
    payload = json.loads(buffer.getvalue().decode("cp950"))

    assert payload == {"ok": True, "text": "日本語"}


def test_runner_loads_the_staged_pp_ocr_adapter(tmp_path: Path, monkeypatch) -> None:
    runner_path = Path(__file__).resolve().parents[2] / "tools" / "ocr_runner.py"
    spec = importlib.util.spec_from_file_location("staged_pp_runner", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    staged_runner = tmp_path / "ocr_runner.py"
    staged_runner.touch()
    staged_runner.with_name("pp_ocr.py").write_text(
        "def create_ocr_reader(model_path, **options):\n"
        "    return lambda image_path: "
        "f'{model_path.name}:{options[\"batch_size\"]}:{image_path}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "__file__", str(staged_runner))

    reader = module._pp_ocr_factory(tmp_path / "model", batch_size=8)

    assert reader("image.png") == "model:8:image.png"


def _pictures(tmp_path: Path, count: int = 2) -> list[str]:
    from PIL import Image

    paths = []
    for index in range(count):
        path = tmp_path / f"pic{index}.png"
        Image.new("RGB", (100, 100), (20, 30, 40)).save(path)
        paths.append(str(path))
    return paths


def test_runner_detects_boxes_without_reading_a_word(tmp_path: Path) -> None:
    paths = _pictures(tmp_path)
    requests = io.StringIO(json.dumps({"action": "detect", "paths": paths}) + "\n")
    responses = io.StringIO()

    class Detector:
        batch_size = 8

        def read_many(self, image_paths: list[str]) -> list[str]:
            found = json.dumps(
                [{"bbox_2d": [10, 10, 90, 40], "text": "ignored", "score": 1.0}]
            )
            return [
                found if index == 0 else json.dumps([])
                for index, _ in enumerate(image_paths)
            ]

    _serve()(
        tmp_path / "model",
        requests,
        responses,
        reader_factory=lambda model_path, **options: Detector(),
    )

    assert json.loads(responses.getvalue()) == {
        "ok": True,
        "boxes": [[[10, 10, 90, 40]], []],
    }


def test_runner_reads_boxes_it_is_handed_without_detecting_again(tmp_path: Path) -> None:
    paths = _pictures(tmp_path, 1)
    boxes = [[[0, 0, 500, 500], [500, 500, 1000, 1000]]]
    requests = io.StringIO(
        json.dumps({"action": "read", "paths": paths, "boxes": boxes}) + "\n"
    )
    responses = io.StringIO()
    seen: list[list[str]] = []

    class Reader:
        batch_size = 8

        def scored_many(self, image_paths: list[str]) -> list[tuple[str, float]]:
            seen.append(list(image_paths))
            return [(Path(path).stem, 1.0) for path in image_paths]

    _serve()(
        tmp_path / "model",
        requests,
        responses,
        reader_factory=lambda model_path, **options: Reader(),
    )

    assert [Path(crop).name for crop in seen[0]] == ["0-0.png", "0-1.png"]
    assert json.loads(responses.getvalue()) == {
        "ok": True,
        "texts": [
            json.dumps(
                [
                    {"bbox_2d": [0, 0, 500, 500], "text": "0-0", "score": 1.0},
                    {
                        "bbox_2d": [500, 500, 1000, 1000],
                        "text": "0-1",
                        "score": 1.0,
                    },
                ],
                ensure_ascii=False,
            )
        ],
    }


def _runner():
    runner_path = Path(__file__).resolve().parents[2] / "tools" / "ocr_runner.py"
    spec = importlib.util.spec_from_file_location("bokucho_ocr_runner_halves", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_detector_requires_score_strictly_above_its_threshold(tmp_path: Path) -> None:
    module = _runner()

    class Adapter:
        batch_size = 2

        def read_many(self, image_paths: list[str]) -> list[str]:
            return [
                json.dumps(
                    [
                        {"bbox_2d": [1, 2, 3, 4], "score": 0.9},
                        {"bbox_2d": [5, 6, 7, 8], "score": 0.91},
                    ]
                )
                for _ in image_paths
            ]

    locating = module.LocatingReader(Adapter(), detection_threshold=0.9)

    assert locating.detect_many([tmp_path / "a.png"]) == [[[5, 6, 7, 8]]]


def test_recognizer_requires_score_strictly_above_its_threshold(tmp_path: Path) -> None:
    module = _runner()
    paths = _pictures(tmp_path, 1)

    class Adapter:
        batch_size = 4

        def scored_many(self, image_paths: list[str]) -> list[tuple[str, float]]:
            assert len(image_paths) == 2
            return [("equal", 0.9), ("above", 0.91)]

    reading = module.ReadingReader(Adapter(), recognition_threshold=0.9)

    [answer] = reading.read_located(
        paths,
        [[[0, 0, 500, 500], [500, 500, 1000, 1000]]],
    )

    assert json.loads(answer) == [
        {"bbox_2d": [500, 500, 1000, 1000], "text": "above", "score": 0.91}
    ]


def test_recognizer_rejects_a_non_finite_score(tmp_path: Path) -> None:
    module = _runner()
    paths = _pictures(tmp_path, 1)

    class Adapter:
        batch_size = 1

        def scored_many(self, image_paths: list[str]) -> list[tuple[str, float]]:
            return [("text", float("nan")) for _ in image_paths]

    reading = module.ReadingReader(Adapter(), recognition_threshold=0.9)

    with pytest.raises(ValueError, match="recognition score"):
        reading.read_located(paths, [[[0, 0, 1000, 1000]]])


def test_a_crop_that_decodes_to_nothing_is_dropped_rather_than_scored(
    tmp_path: Path,
) -> None:
    module = _runner()
    paths = _pictures(tmp_path, 1)

    class Adapter:
        batch_size = 2

        def scored_many(self, image_paths: list[str]) -> list[tuple[str, float]]:
            return [("", float("nan")), ("読んだ", 1.0)]

    reading = module.ReadingReader(Adapter(), recognition_threshold=0.9)

    [answer] = reading.read_located(
        paths,
        [[[0, 0, 500, 500], [500, 500, 1000, 1000]]],
    )

    assert json.loads(answer) == [
        {"bbox_2d": [500, 500, 1000, 1000], "text": "読んだ", "score": 1.0}
    ]


class _Card:
    """A model that refuses any batch bigger than it has room for."""

    batch_size = 8

    def __init__(self, holds: int) -> None:
        self.holds = holds
        self.attempts: list[int] = []

    def read_many(self, image_paths: list[str]) -> list[str]:
        self.attempts.append(len(image_paths))
        if len(image_paths) > self.holds:
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        return [Path(path).stem for path in image_paths]


def test_a_refused_batch_is_asked_again_in_half_the_company() -> None:
    module = _runner()
    card = _Card(holds=2)
    batches = module._Batches(8)

    answers = batches(card, [f"{index}.png" for index in range(8)])

    assert answers == [(str(index), None) for index in range(8)]
    assert card.attempts == [8, 4, 2, 2, 2, 2]
    assert batches.size == 3


def test_a_refusal_is_climbed_back_towards_and_then_settled_on() -> None:
    module = _runner()
    card = _Card(holds=5)
    batches = module._Batches(16)

    for _ in range(12):
        batches(card, [f"{index}.png" for index in range(16)])

    assert batches.size == 5
    assert sorted(size for size in card.attempts if size > card.holds) == [6, 7, 8, 16]


def test_a_batch_climbs_back_while_the_card_keeps_taking_it() -> None:
    module = _runner()
    card = _Card(holds=99)
    batches = module._Batches(2)

    for _ in range(5):
        batches(card, ["a.png", "b.png"])

    assert batches.size == 4


def test_a_failure_that_is_not_a_refusal_is_not_retried_smaller() -> None:
    module = _runner()

    class Broken:
        batch_size = 4

        def read_many(self, image_paths: list[str]) -> list[str]:
            raise ValueError("the checkpoint is missing a tokenizer")

    with pytest.raises(ValueError):
        module._Batches(4)(Broken(), ["a.png", "b.png"])


def _spotted(*boxes: list[int]) -> str:
    """A detector's answer naming ``boxes``, each comfortably above threshold."""
    return json.dumps(
        [{"bbox_2d": box, "score": 0.99} for box in boxes]
    )


def test_boxes_come_back_in_reading_order() -> None:
    module = _runner()
    top_left = [0, 0, 400, 100]
    top_right = [500, 0, 900, 100]
    middle = [0, 200, 900, 300]
    bottom = [0, 400, 400, 500]

    answer = _spotted(bottom, top_right, middle, top_left)

    assert module._located_boxes(answer, 0.5) == [top_left, top_right, middle, bottom]


def test_a_row_is_read_left_to_right_before_the_row_below() -> None:
    module = _runner()
    left_top = [0, 0, 300, 90]
    right_top = [500, 5, 800, 95]
    left_next = [0, 120, 300, 210]
    right_next = [500, 125, 800, 215]

    answer = _spotted(right_next, left_top, right_top, left_next)

    assert module._located_boxes(answer, 0.5) == [
        left_top,
        right_top,
        left_next,
        right_next,
    ]


def test_a_box_that_rounds_away_is_not_cut_into_a_crop(tmp_path: Path) -> None:
    module = _runner()
    from PIL import Image

    picture = tmp_path / "chip.png"
    Image.new("RGB", (100, 100), (20, 30, 40)).save(picture)
    readable = [100, 100, 900, 400]
    rounds_away = [500, 500, 510, 510]

    crops, owners = _cut(module, [str(picture)], [[readable, rounds_away]], tmp_path)

    assert owners == [(0, readable)]
    assert len(crops) == 1


def _cut(module, paths: list[str], boxes, scratch: Path):
    target = scratch / "crops"
    target.mkdir()
    return module._cut_crops(paths, boxes, target)
