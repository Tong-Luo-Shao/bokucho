from __future__ import annotations

from pathlib import Path

from PIL import Image

from bokucho.infrastructure import pp_ocr


def _half(batch_size: int) -> pp_ocr._PpOcrHalf:
    """A half with a batch size and nothing else -- no card, no checkpoint."""
    half = object.__new__(pp_ocr._PpOcrHalf)
    half.batch_size = batch_size
    return half


def _pictures(tmp_path: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        path = tmp_path / f"{index}.png"
        Image.new("RGB", (10, 10)).save(path)
        paths.append(str(path))
    return paths


def test_more_pictures_than_the_batch_reach_the_card_in_batches(
    tmp_path: Path,
) -> None:
    half = _half(8)
    chunks: list[int] = []

    def of_a_chunk(paths: list[str]) -> list[str]:
        chunks.append(len(paths))
        return paths

    paths = _pictures(tmp_path, 20)
    answers = half._in_batches(paths, of_a_chunk)

    assert chunks == [8, 8, 4]
    assert answers == paths
