from __future__ import annotations

import argparse
import html
import importlib.util
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import TextIO

OcrReader = Callable[[str], object]
OcrReaderFactory = Callable[..., OcrReader]


def _force_offline_environment() -> None:
    os.environ.update(
        {
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )


def _configure_standard_streams() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _staged_adapter_factory(
    filename: str,
    module_name: str,
    display_name: str,
    model_path: Path,
    *,
    batch_size: int,
) -> OcrReader:
    adapter_path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, adapter_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the {display_name} adapter at {adapter_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    factory = getattr(module, "create_ocr_reader", None)
    if not callable(factory):
        raise RuntimeError(f"{display_name} adapter has no reader factory: {adapter_path}")
    return factory(model_path, batch_size=batch_size)


def _pp_ocr_factory(
    model_path: Path,
    **options,
) -> OcrReader:
    return _staged_adapter_factory(
        "pp_ocr.py",
        "bokucho_pp_ocr",
        "PP-OCR",
        model_path,
        **options,
    )


_BATCHES_BEFORE_GROWING = 4
_BATCH_GROWTH = 4


def _out_of_memory(exc: BaseException) -> bool:
    """Whether the card refused the batch rather than the model failing."""
    if type(exc).__name__ == "OutOfMemoryError":
        return True
    return "out of memory" in str(exc).lower()


def _release_cached_memory() -> None:
    """Hand back what the refused batch reserved, if there is a card to hand to."""
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


class _Batches:
    """Work handed to a model in the largest company the card is taking."""

    def __init__(self, size: int) -> None:
        requested = max(1, int(size or 1))
        self._ceiling = requested * _BATCH_GROWTH
        self._size = requested
        self._clean = 0

    @property
    def size(self) -> int:
        return self._size

    def __call__(
        self, reader: OcrReader, items: Sequence[str]
    ) -> list[tuple[str, float | None]]:
        work = list(items)
        answers: list[tuple[str, float | None]] = []
        while len(answers) < len(work):
            chunk = work[len(answers) : len(answers) + self._size]
            try:
                answers.extend(_recognize_many(reader, chunk))
            except Exception as exc:  # noqa: BLE001 - a refusal is answered, not raised
                if self._size == 1 or not _out_of_memory(exc):
                    raise
                self._refused()
            else:
                self._taken(len(chunk))
        return answers

    def _refused(self) -> None:
        self._ceiling = max(1, self._size - 1)
        self._size = max(1, self._size // 2)
        self._clean = 0
        _release_cached_memory()

    def _taken(self, taken: int) -> None:
        if taken < self._size or self._size >= self._ceiling:
            return
        self._clean += 1
        if self._clean >= _BATCHES_BEFORE_GROWING:
            self._size = min(self._ceiling, self._size * 2)
            self._clean = 0


BOX_SCALE = 1000
BOX_OVERLAP_SHARE = 0.5
MIN_CROP_PIXELS = 2
_MARKUP_RE = re.compile(r"<[^>]*>")
_LINE_BREAK_RE = re.compile(r"<br\s*/?>|</p>|</div>|</h[1-6]>")


def _plain_reading(answer: str) -> str:
    """The words in ``answer``, with any layout the reader wrapped them in gone."""
    if "<" not in answer:
        return answer.strip()
    broken = _LINE_BREAK_RE.sub("\n", answer)
    text = html.unescape(_MARKUP_RE.sub("", broken))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _confidence(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise ValueError(f"{label} must be a finite number from 0 to 1")
    return float(value)


def _located_boxes(answer: str, detection_threshold: float) -> list[list[int]]:
    """The boxes in a spotting answer, in the 0-1000 scale it reported them on."""
    try:
        parsed = json.loads(answer)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    boxes = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        box = entry.get("bbox_2d")
        if (
            isinstance(box, list)
            and len(box) == 4
            and all(isinstance(value, int | float) for value in box)
        ):
            score = _confidence(entry.get("score"), "detection score")
            if score > detection_threshold:
                boxes.append([int(value) for value in box])
    return _in_reading_order(_untangled(boxes))


def _in_reading_order(boxes: list[list[int]]) -> list[list[int]]:
    """``boxes`` as a person reads them: each row left to right, top row first."""
    rows: list[list[list[int]]] = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        for row in rows:
            first = row[0]
            overlap = min(box[3], first[3]) - max(box[1], first[1])
            shorter = min(box[3] - box[1], first[3] - first[1])
            if shorter > 0 and overlap / shorter > 0.5:
                row.append(box)
                break
        else:
            rows.append([box])
    return [box for row in rows for box in sorted(row, key=lambda b: b[0])]


def _box_area(box: list[int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _covered_share(box: list[int], other: list[int]) -> float:
    """How much of ``box`` lies inside ``other``."""
    width = min(box[2], other[2]) - max(box[0], other[0])
    height = min(box[3], other[3]) - max(box[1], other[1])
    area = _box_area(box)
    return max(0, width) * max(0, height) / area if area else 0.0


def _untangled(boxes: list[list[int]]) -> list[list[int]]:
    """Boxes cut back where one has swallowed a neighbour, duplicates dropped."""
    cut = [list(box) for box in boxes]
    for here, outer in enumerate(cut):
        for there, inner in enumerate(boxes):
            if here == there or _covered_share(inner, outer) <= BOX_OVERLAP_SHARE:
                continue
            if inner[1] > outer[1] and inner[3] > outer[3]:
                outer[3] = inner[1]
            elif inner[3] < outer[3] and inner[1] < outer[1]:
                outer[1] = inner[3]
    keep: list[int] = []
    for here, box in enumerate(cut):
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        if any(
            _covered_share(box, cut[there]) > BOX_OVERLAP_SHARE
            and _box_area(cut[there]) > _box_area(box)
            for there in range(len(cut))
            if there != here
        ):
            continue
        keep.append(here)
    return [cut[index] for index in keep]


class LocatedReader:
    """One model says where the text is, another says what it says."""

    def __init__(
        self,
        detector: OcrReader,
        reader: OcrReader,
        crop_batch_size: int | None = None,
        detection_threshold: float = 0.9,
        recognition_threshold: float = 0.9,
    ) -> None:
        self._detector = detector
        self._reader = reader
        self._detect_batch = _Batches(getattr(detector, "batch_size", 1))
        self._crop_batch = _Batches(
            crop_batch_size or getattr(reader, "batch_size", 1)
        )
        self._detection_threshold = _confidence(
            detection_threshold, "detection threshold"
        )
        self._recognition_threshold = _confidence(
            recognition_threshold, "recognition threshold"
        )
        self.batch_size = self._detect_batch.size

    @property
    def runtime_report(self) -> dict[str, object]:
        report = dict(getattr(self._reader, "runtime_report", {}) or {})
        located = getattr(self._detector, "runtime_report", {}) or {}
        report["detector_memory_bytes"] = located.get("model_memory_bytes", 0)
        report["detector_precision"] = located.get("requested_precision", "")
        return report

    def __call__(self, image_path: str | Path) -> str:
        [answer] = self.read_many([image_path])
        return answer

    def detect_many(self, image_paths: Sequence[str | Path]) -> list[list[list[int]]]:
        """Where the text sits on each picture, and nothing about what it says."""
        return _detect_in_batches(
            self._detector,
            [str(path) for path in image_paths],
            self._detect_batch,
            self._detection_threshold,
        )

    def read_located(
        self,
        image_paths: Sequence[str | Path],
        boxes: Sequence[Sequence[Sequence[int]]],
    ) -> list[str]:
        """What the text says, given boxes a detector has already settled."""
        return _read_located(
            self._reader,
            [str(path) for path in image_paths],
            [[list(box) for box in located] for located in boxes],
            self._crop_batch,
            self._recognition_threshold,
        )

    def read_many(self, image_paths: Sequence[str | Path]) -> list[str]:
        paths = [str(path) for path in image_paths]
        if not paths:
            return []
        return self.read_located(paths, self.detect_many(paths))


class _Half:
    """One model of the pair, wearing the interface a two-pass scan asks for."""

    def __init__(self, model: OcrReader) -> None:
        self._model = model

    def __getattr__(self, name: str):
        return getattr(self._model, name)


class LocatingReader(_Half):
    """A detector on its own: where the text is, and never what it says."""

    def __init__(self, model: OcrReader, detection_threshold: float = 0.9) -> None:
        super().__init__(model)
        self._detect_batch = _Batches(getattr(model, "batch_size", 1))
        self._detection_threshold = _confidence(
            detection_threshold, "detection threshold"
        )

    def detect_many(self, image_paths: Sequence[str | Path]) -> list[list[list[int]]]:
        return _detect_in_batches(
            self._model,
            [str(path) for path in image_paths],
            self._detect_batch,
            self._detection_threshold,
        )


class ReadingReader(_Half):
    """A reader on its own: handed boxes, it says what is inside them."""

    def __init__(
        self,
        model: OcrReader,
        crop_batch_size: int | None = None,
        recognition_threshold: float = 0.9,
    ) -> None:
        super().__init__(model)
        self._crop_batch = _Batches(
            crop_batch_size or getattr(model, "batch_size", 1)
        )
        self._recognition_threshold = _confidence(
            recognition_threshold, "recognition threshold"
        )

    def read_located(
        self,
        image_paths: Sequence[str | Path],
        boxes: Sequence[Sequence[Sequence[int]]],
    ) -> list[str]:
        return _read_located(
            self._model,
            [str(path) for path in image_paths],
            [[list(box) for box in located] for located in boxes],
            self._crop_batch,
            self._recognition_threshold,
        )


def _detect_in_batches(
    detector: OcrReader,
    paths: list[str],
    batches: _Batches,
    detection_threshold: float,
) -> list[list[list[int]]]:
    """Boxes for every path, asked in the company the detector is taking."""
    return [
        _located_boxes(answer, detection_threshold)
        for answer, _ in batches(detector, paths)
    ]


def _read_located(
    reader: OcrReader,
    paths: list[str],
    boxes: list[list[list[int]]],
    crop_batch: _Batches,
    recognition_threshold: float,
) -> list[str]:
    """One answer per path: the detector's boxes carrying the reader's words."""
    if not paths:
        return []
    with tempfile.TemporaryDirectory(prefix="bokucho-crop-") as scratch:
        crops, owners = _cut_crops(paths, boxes, Path(scratch))
        readings = crop_batch(reader, crops)
    answers: list[list[dict[str, object]]] = [[] for _ in paths]
    for (index, box), (text, score) in zip(owners, readings, strict=True):
        reading = _plain_reading(text)
        if not reading:
            continue
        confidence = _confidence(score, "recognition score")
        if confidence <= recognition_threshold:
            continue
        answers[index].append({"bbox_2d": box, "text": reading, "score": confidence})
    return [json.dumps(entries, ensure_ascii=False) for entries in answers]


def _cut_crops(
    paths: list[str],
    boxes: list[list[list[int]]],
    scratch: Path,
) -> tuple[list[str], list[tuple[int, list[int]]]]:
    from PIL import Image

    crops: list[str] = []
    owners: list[tuple[int, list[int]]] = []
    for index, (path, located) in enumerate(zip(paths, boxes, strict=True)):
        if not located:
            continue
        with Image.open(path) as source:
            picture = source.convert("RGB")
            picture.load()
        width, height = picture.size
        for order, box in enumerate(located):
            x0 = max(0, min(width, box[0] * width // BOX_SCALE))
            y0 = max(0, min(height, box[1] * height // BOX_SCALE))
            x1 = max(0, min(width, box[2] * width // BOX_SCALE))
            y1 = max(0, min(height, box[3] * height // BOX_SCALE))
            if x1 - x0 < MIN_CROP_PIXELS or y1 - y0 < MIN_CROP_PIXELS:
                continue
            cut = picture.crop((x0, y0, x1, y1))
            target = scratch / f"{index}-{order}.png"
            cut.save(target)
            cut.close()
            crops.append(str(target))
            owners.append((index, box))
        picture.close()
    return crops, owners


def _write_response(output_stream: TextIO, payload: dict[str, object]) -> None:
    output_stream.write(json.dumps(payload) + "\n")
    output_stream.flush()


def _recognize_many(
    reader: OcrReader, image_paths: list[str]
) -> list[tuple[str, float | None]]:
    """What each picture says, and how sure the reader is, where it says so."""
    scored_many = getattr(reader, "scored_many", None)
    if callable(scored_many):
        answers = [
            (str(text), float(score)) for text, score in scored_many(image_paths)
        ]
    else:
        read_many = getattr(reader, "read_many", None)
        if callable(read_many):
            values = read_many(image_paths)
        else:
            values = [reader(image_path) for image_path in image_paths]
        answers = [(str(value), None) for value in values]
    if len(answers) != len(image_paths):
        raise RuntimeError("OCR reader returned the wrong number of results")
    return answers


def _paths_in(request: dict) -> list[str]:
    paths = request.get("paths")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise ValueError("image paths must be a list of strings")
    return paths


def _boxes_in(request: dict, count: int) -> list[list[list[int]]]:
    """One list of boxes per path, as the detector left them."""
    boxes = request.get("boxes")
    if not isinstance(boxes, list) or len(boxes) != count:
        raise ValueError("boxes must be one list per image path")
    located: list[list[list[int]]] = []
    for entry in boxes:
        if not isinstance(entry, list):
            raise ValueError("boxes must be one list per image path")
        for box in entry:
            if not isinstance(box, list) or len(box) != 4 or not all(
                isinstance(value, int | float) for value in box
            ):
                raise ValueError("every box must be four numbers")
        located.append([[int(value) for value in box] for box in entry])
    return located


def serve(
    model_path: Path,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    batch_size: int = 8,
    reader_factory: OcrReaderFactory = _pp_ocr_factory,
    detector_path: Path | None = None,
    detector_factory: OcrReaderFactory | None = None,
    detector_batch_size: int = 8,
    crop_batch_size: int | None = None,
    detection_threshold: float = 0.9,
    recognition_threshold: float = 0.9,
) -> None:
    _force_offline_environment()
    reader: OcrReader | None = None
    detect_batch: _Batches | None = None
    read_batch: _Batches | None = None
    for line in input_stream:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                if reader is None:
                    located = detector_factory is not None and detector_path is not None
                    reader = reader_factory(model_path, batch_size=batch_size)
                    if located:
                        reader = LocatedReader(
                            detector_factory(
                                detector_path, batch_size=detector_batch_size
                            ),
                            reader,
                            crop_batch_size,
                            detection_threshold,
                            recognition_threshold,
                        )
                    detect_batch = _Batches(getattr(reader, "batch_size", 1))
                    read_batch = _Batches(
                        crop_batch_size or getattr(reader, "batch_size", 1)
                    )
                if request.get("action") == "status":
                    runtime = getattr(reader, "runtime_report", None)
                    if not isinstance(runtime, dict):
                        raise RuntimeError("OCR reader has no verified runtime report")
                    response = {"ok": True, "runtime": runtime}
                elif request.get("action") == "detect":
                    response = {
                        "ok": True,
                        "boxes": _detect_in_batches(
                            reader,
                            _paths_in(request),
                            detect_batch,
                            detection_threshold,
                        ),
                    }
                elif request.get("action") == "read":
                    image_paths = _paths_in(request)
                    response = {
                        "ok": True,
                        "texts": _read_located(
                            reader,
                            image_paths,
                            _boxes_in(request, len(image_paths)),
                            read_batch,
                            recognition_threshold,
                        ),
                    }
                elif "paths" in request:
                    response = {
                        "ok": True,
                        "texts": [
                            text
                            for text, _ in _recognize_many(reader, _paths_in(request))
                        ],
                    }
                else:
                    image_path = request["path"]
                    if not isinstance(image_path, str):
                        raise ValueError("image path must be a string")
                    response = {"ok": True, "text": str(reader(image_path))}
        except Exception as exc:  # noqa: BLE001 - failures cross the process boundary
            message = str(exc).strip() or type(exc).__name__
            _write_response(output_stream, {"ok": False, "error": message})
        else:
            _write_response(output_stream, response)


def main() -> int:
    _configure_standard_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--detector-model", type=Path)
    parser.add_argument("--detector-batch-size", type=int, default=8)
    parser.add_argument("--crop-batch-size", type=int)
    parser.add_argument("--detection-threshold", type=float, default=0.9)
    parser.add_argument("--recognition-threshold", type=float, default=0.9)
    args = parser.parse_args()
    serve(
        args.model,
        sys.stdin,
        sys.stdout,
        batch_size=args.batch_size,
        reader_factory=_pp_ocr_factory,
        detector_path=args.detector_model,
        detector_factory=_pp_ocr_factory if args.detector_model else None,
        detector_batch_size=args.detector_batch_size,
        crop_batch_size=args.crop_batch_size,
        detection_threshold=args.detection_threshold,
        recognition_threshold=args.recognition_threshold,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
