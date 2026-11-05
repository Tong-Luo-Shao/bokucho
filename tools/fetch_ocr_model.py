"""Stage the fixed PP-OCR checkpoints and the eraser for a fully offline portable release."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bokucho import render as render_mod  # noqa: E402
from bokucho.images import is_complete_ocr_snapshot  # noqa: E402
from bokucho.infrastructure.pp_ocr import (  # noqa: E402
    DETECTOR_CHECKPOINT,
    RECOGNIZER_CHECKPOINT,
    PpOcrCheckpoint,
)

PORTABLE_DIR = ROOT / "dist" / "Bokucho-Windows-x64"
CHECKPOINTS = (DETECTOR_CHECKPOINT, RECOGNIZER_CHECKPOINT)
Downloader = Callable[[str, str], Path]
EXECUTABLE_NAME = "Bokucho.exe"


def _huggingface_downloader(model_id: str, revision: str) -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(model_id, revision=revision))


def resolve_release_dir(candidate: Path) -> Path:
    if not candidate.is_dir():
        raise ValueError(f"No release directory at {candidate}; build it first.")
    if not (candidate / EXECUTABLE_NAME).is_file():
        raise ValueError(
            f"{candidate} holds no {EXECUTABLE_NAME}, so it is not a portable "
            "release. Run tools/build.py first, and pass --release-dir from "
            "the repository root."
        )
    return candidate


def _stage_checkpoint(
    release_dir: Path,
    checkpoint: PpOcrCheckpoint,
    downloader: Downloader,
) -> Path:
    destination = release_dir / "models" / checkpoint.dirname
    if is_complete_ocr_snapshot(destination, checkpoint):
        return destination

    snapshot = downloader(checkpoint.model_id, checkpoint.revision)
    if not is_complete_ocr_snapshot(snapshot, checkpoint):
        raise RuntimeError(f"Downloaded snapshot is incomplete: {snapshot}.")

    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    for item in snapshot.iterdir():
        if not item.is_file():
            continue
        target = staging / item.name
        try:
            os.link(item.resolve(), target)
        except OSError:
            shutil.copy2(item, target)
    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    return destination


def stage_models(
    release_dir: Path,
    downloader: Downloader = _huggingface_downloader,
) -> tuple[Path, Path]:
    return tuple(
        _stage_checkpoint(release_dir, checkpoint, downloader)
        for checkpoint in CHECKPOINTS
    )


def stage_eraser(
    release_dir: Path,
    fetch: Callable[..., Path] = render_mod.download_model,
) -> Path:
    """Stage the pinned eraser beside the readers, from the user cache."""
    destination = release_dir / "models" / render_mod.MODEL_NAME
    if destination.is_file():
        return destination
    source = fetch(None)
    staging = destination.with_name(destination.name + ".partial")
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.unlink(missing_ok=True)
    try:
        os.link(source, staging)
    except OSError:
        shutil.copy2(source, staging)
    staging.replace(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=PORTABLE_DIR,
        help=f"Portable release directory (default: {PORTABLE_DIR}).",
    )
    arguments = parser.parse_args()
    try:
        release_dir = resolve_release_dir(arguments.release_dir)
    except ValueError as error:
        parser.error(str(error))
    for checkpoint, staged in zip(CHECKPOINTS, stage_models(release_dir), strict=True):
        print(f"Staged {checkpoint.model_id}@{checkpoint.revision[:8]} -> {staged}")
    eraser = stage_eraser(release_dir)
    print(f"Staged {render_mod.MODEL_NAME}@{render_mod.MODEL_SHA256[:8]} -> {eraser}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
