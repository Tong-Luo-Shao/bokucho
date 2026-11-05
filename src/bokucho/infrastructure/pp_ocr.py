"""Offline Transformers adapter for the pinned PP-OCR detector and recognizer."""

from __future__ import annotations

import gc
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

BOX_SCALE = 1000
_MASK_THRESHOLD = 0.5
_MAX_SIDE = 1024
_DETECTOR_SUFFIX = "_det"
_RECOGNIZER_SUFFIX = "_rec"
DEFAULT_BATCH_SIZE = 8
VISUAL_FLOOR_PIXELS = 0


@dataclass(frozen=True, slots=True)
class PpOcrCheckpoint:
    model_id: str
    revision: str
    dirname: str
    manifest: tuple[tuple[str, int], ...]

    @property
    def weights(self) -> frozenset[str]:
        return frozenset(
            name for name, _size in self.manifest if name.endswith(".safetensors")
        )

    @property
    def files(self) -> frozenset[str]:
        return frozenset(name for name, _size in self.manifest if name not in self.weights)

    @property
    def download_bytes(self) -> int:
        return sum(size for _name, size in self.manifest)


DETECTOR_CHECKPOINT = PpOcrCheckpoint(
    model_id="PaddlePaddle/PP-OCRv6_medium_det_safetensors",
    revision="4236c2b61741a259c091fd879dcc4edc339e916c",
    dirname="pp-ocrv6-medium-det",
    manifest=(
        (".gitattributes", 1_519),
        ("README.md", 23_399),
        ("config.json", 2_134),
        ("inference.yml", 886),
        ("model.safetensors", 88_020_412),
        ("preprocessor_config.json", 838),
    ),
)

RECOGNIZER_CHECKPOINT = PpOcrCheckpoint(
    model_id="PaddlePaddle/PP-OCRv6_medium_rec_safetensors",
    revision="024cad6a831de75c2c3c26e711ba8c4a82ccd24b",
    dirname="pp-ocrv6-medium-rec",
    manifest=(
        (".gitattributes", 1_519),
        ("README.md", 23_787),
        ("config.json", 1_131),
        ("inference.yml", 150_580),
        ("model.safetensors", 76_741_720),
        ("preprocessor_config.json", 206_177),
    ),
)


def _model_type(model_path: Path) -> str:
    """What the checkpoint calls itself, read from the snapshot rather than told."""
    config = json.loads((Path(model_path) / "config.json").read_text(encoding="utf-8"))
    model_type = config.get("model_type")
    if not isinstance(model_type, str) or not model_type:
        raise ValueError(f"PP-OCR checkpoint has no model_type: {model_path}")
    return model_type


class _PpOcrHalf:
    """What the two halves share: a snapshot, a card, and a report about both."""

    def __init__(self, model_path: Path, batch_size: int) -> None:
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError(
                "PP-OCR requires an NVIDIA CUDA GPU and a CUDA-enabled PyTorch "
                "runtime."
            )
        if batch_size < 1:
            raise ValueError("OCR batch size must be positive.")
        self._torch = torch
        self.batch_size = batch_size
        self.visual_floor_pixels = 0
        self._model: object | None = None
        self._processor: object | None = None

    def _load(self, model_path: Path, suffix: str, kind: str, model_class: type) -> None:
        from transformers import AutoImageProcessor

        model_type = _model_type(model_path)
        if not model_type.endswith(suffix):
            raise ValueError(f"Not a PP-OCR {kind} checkpoint: {model_type}")
        snapshot = str(model_path)
        self._processor = AutoImageProcessor.from_pretrained(snapshot, local_files_only=True)
        self._model = model_class.from_pretrained(snapshot, local_files_only=True).to("cuda")
        self._model.eval()
        self._runtime_report = self._report(self._model, model_type)

    @property
    def runtime_report(self) -> dict[str, object]:
        return dict(self._runtime_report)

    def __call__(self, image_path: str | Path) -> str:
        [answer] = self.read_many([image_path])
        return answer

    def close(self) -> None:
        """Give the card its weights back."""
        if self._model is None:
            return
        self._model = None
        self._processor = None
        gc.collect()
        self._torch.cuda.empty_cache()

    def _report(self, model: object, model_type: str) -> dict[str, object]:
        return {
            "requested_precision": "FP32",
            "weight_format": "FP32",
            "compute_dtype": "FP32",
            "quantized_linear_layers": 0,
            "model_memory_bytes": int(model.get_memory_footprint()),
            "vram_budget_bytes": int(
                self._torch.cuda.get_device_properties(0).total_memory
            ),
            "model_type": model_type,
        }

    @staticmethod
    def _pictures(image_paths: Sequence[str | Path]) -> list[Image.Image]:
        pictures = []
        for image_path in image_paths:
            with Image.open(image_path) as source:
                picture = source.convert("RGB")
                picture.load()
            pictures.append(picture)
        return pictures

    def _in_batches(self, image_paths: Sequence[str | Path], of_a_chunk):
        """``of_a_chunk`` over pictures of one size, ``batch_size`` at a time."""
        answers: list[object] = [None] * len(image_paths)
        groups: dict[tuple[int, int], list[int]] = {}
        for index, image_path in enumerate(image_paths):
            with Image.open(image_path) as probe:
                shape = probe.size
            groups.setdefault(shape, []).append(index)
        size = self.batch_size
        for indexes in groups.values():
            for start in range(0, len(indexes), size):
                chunk = indexes[start : start + size]
                read = of_a_chunk([image_paths[index] for index in chunk])
                for index, answer in zip(chunk, read, strict=True):
                    answers[index] = answer
        return answers


class PpOcrDetector(_PpOcrHalf):
    """Where the text is on a whole picture, and nothing about what it says."""

    def __init__(self, model_path: Path, *, batch_size: int = 1) -> None:
        from transformers.utils import is_cv2_available

        if not is_cv2_available():
            raise RuntimeError(
                "The PP-OCR detector needs OpenCV, which this OCR runtime was "
                "staged without. Reinstall the OCR runtime to pick it up."
            )
        super().__init__(model_path, batch_size)
        from transformers import AutoModelForObjectDetection

        self._load(model_path, _DETECTOR_SUFFIX, "detector", AutoModelForObjectDetection)

    def read_many(self, image_paths: Sequence[str | Path]) -> list[str]:
        if not image_paths:
            return []
        return self._in_batches(image_paths, self._locate_chunk)

    def _locate_chunk(self, image_paths: list[str | Path]) -> list[str]:
        pictures = self._pictures(image_paths)
        try:
            sizes = [(picture.height, picture.width) for picture in pictures]
            inputs = self._processor(
                images=pictures,
                return_tensors="pt",
                max_side_limit=max(_MAX_SIDE, *(max(size) for size in sizes)),
            ).to("cuda")
            with self._torch.inference_mode():
                predictions = self._model(**inputs)
            located = self._processor.post_process_object_detection(
                predictions,
                threshold=_MASK_THRESHOLD,
                target_sizes=self._torch.tensor(sizes, device="cuda"),
            )
        finally:
            for picture in pictures:
                picture.close()
        return [
            _scaled_regions(
                found["boxes"].tolist(),
                found["scores"].tolist(),
                width,
                height,
            )
            for found, (height, width) in zip(located, sizes, strict=True)
        ]


class PpOcrRecognizer(_PpOcrHalf):
    """What one strip of text says, given a crop somebody else located."""

    def __init__(self, model_path: Path, *, batch_size: int = 1) -> None:
        super().__init__(model_path, batch_size)
        from transformers import AutoModelForTextRecognition

        self._load(model_path, _RECOGNIZER_SUFFIX, "recognizer", AutoModelForTextRecognition)

    def read_many(self, image_paths: Sequence[str | Path]) -> list[str]:
        return [text for text, _ in self.scored_many(image_paths)]

    def scored_many(
        self, image_paths: Sequence[str | Path]
    ) -> list[tuple[str, float]]:
        """What each crop says, and how sure the recognizer is that it says it."""
        if not image_paths:
            return []
        return self._in_batches(image_paths, self._read_chunk)

    def _read_chunk(self, image_paths: list[str | Path]) -> list[tuple[str, float]]:
        crops = self._pictures(image_paths)
        try:
            inputs = self._processor(images=crops, return_tensors="pt").to("cuda")
            with self._torch.inference_mode():
                predictions = self._model(**inputs)
            read = self._processor.post_process_text_recognition(predictions)
        finally:
            for crop in crops:
                crop.close()
        return [
            (str(entry.get("text", "")), float(entry.get("score", 0.0)))
            for entry in read
        ]


def _scaled_regions(
    quads: list[list[list[float]]],
    scores: list[float],
    width: int,
    height: int,
) -> str:
    """The detector's quads as the 0-1000 boxes the rest of the scan reads."""
    regions = []
    for quad, score in zip(quads, scores, strict=True):
        xs = [corner[0] for corner in quad]
        ys = [corner[1] for corner in quad]
        box = [
            _scaled(min(xs), width),
            _scaled(min(ys), height),
            _scaled(max(xs), width),
            _scaled(max(ys), height),
        ]
        if box[0] < box[2] and box[1] < box[3]:
            regions.append({"bbox_2d": box, "score": float(score)})
    return json.dumps(regions, ensure_ascii=False)


def _scaled(value: float, extent: int) -> int:
    return max(0, min(BOX_SCALE, round(value * BOX_SCALE / extent)))


def create_ocr_reader(model_path: Path, *, batch_size: int = 1):
    """The half this snapshot is, built from what the snapshot says it is."""
    model_type = _model_type(model_path)
    if model_type.endswith(_DETECTOR_SUFFIX):
        return PpOcrDetector(model_path, batch_size=batch_size)
    if model_type.endswith(_RECOGNIZER_SUFFIX):
        return PpOcrRecognizer(model_path, batch_size=batch_size)
    raise ValueError(f"Unsupported PP-OCR checkpoint: {model_type}")
