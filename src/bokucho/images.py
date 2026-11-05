"""Image-asset localization — the Phase 7a image track."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import weakref
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

from PIL import Image, ImageStat

from bokucho import gal
from bokucho import render as render_mod
from bokucho.application.contracts import CancellationToken
from bokucho.application.errors import (
    JobCancelled,
    MissingDependency,
    MultiFrameImage,
    OcrRuntimeLost,
)
from bokucho.engines.rpgmv_assets import ENCRYPTED_SUFFIXES, open_bytes
from bokucho.infrastructure.ocr_runtime import (
    CUDA_RUNTIME_DOWNLOAD_BYTES as CUDA_RUNTIME_DOWNLOAD_BYTES,
)
from bokucho.infrastructure.ocr_runtime import (
    OCR_RUNTIME_METADATA_NAME as OCR_RUNTIME_METADATA_NAME,
)
from bokucho.infrastructure.ocr_runtime import (
    OCR_TORCH_BACKEND as OCR_TORCH_BACKEND,
)
from bokucho.infrastructure.ocr_runtime import (
    OCR_TRANSFORMERS_VERSION as OCR_TRANSFORMERS_VERSION,
)
from bokucho.infrastructure.ocr_runtime import (
    RuntimeProgress,
    bootstrap_is_complete,
    install_cuda_runtime,
    runtime_is_complete,
)
from bokucho.infrastructure.pp_ocr import (
    DEFAULT_BATCH_SIZE,
    DETECTOR_CHECKPOINT,
    RECOGNIZER_CHECKPOINT,
    VISUAL_FLOOR_PIXELS,
    PpOcrCheckpoint,
)
from bokucho.infrastructure.pp_ocr import (
    create_ocr_reader as _pp_ocr_reader,
)

try:
    import livemaker.GalImagePlugin  # noqa: F401

    HAS_GAL = True
except ImportError:  # pragma: no cover - exercised only without the extra
    HAS_GAL = False

IMAGE_EXTS = {".gal", ".png", ".jpg", ".jpeg", ".bmp", ".rpgmvp", ".png_"}
IMAGES_SOURCE_NAME = "images"
IMAGES_STRINGS_FILE = IMAGES_SOURCE_NAME + ".json"
OCR_MODEL_ID = RECOGNIZER_CHECKPOINT.model_id
OCR_MODEL_REVISION = RECOGNIZER_CHECKPOINT.revision
OCR_MODEL_DIRNAME = RECOGNIZER_CHECKPOINT.dirname
OCR_MODEL_DOWNLOAD_MANIFEST = RECOGNIZER_CHECKPOINT.manifest
OCR_MODEL_WEIGHTS = RECOGNIZER_CHECKPOINT.weights
OCR_MODEL_FILES = RECOGNIZER_CHECKPOINT.files

DownloadProgress = Callable[[int, int], None]


def _portable_root_dir() -> Path:
    compiled = globals().get("__compiled__")
    containing_dir = getattr(compiled, "containing_dir", None)
    if containing_dir is not None:
        return Path(containing_dir)
    return Path(sys.argv[0]).resolve().parent


def unit_id(rel: str) -> str:
    return f"img:{rel}"


def iter_images(root: Path) -> list[Path]:
    """Every image file under ``root`` (sorted, recursive)."""
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXTS)


def _openable(path: Path) -> Path | io.BytesIO:
    """What PIL can open for ``path`` -- itself, or its decrypted bytes."""
    if path.suffix.lower() in ENCRYPTED_SUFFIXES:
        return io.BytesIO(open_bytes(path))
    return path


def inspect(path: Path) -> dict:
    """Lightweight metadata for one image."""
    with Image.open(_openable(path)) as img:
        return {
            "format": img.format,
            "width": img.size[0],
            "height": img.size[1],
            "mode": img.mode,
        }


def frame_count(path: Path) -> int:
    """Frames held by ``path``, counted the way the format allows."""
    if path.suffix.lower() == ".gal":
        return gal.frame_count(path)
    try:
        with Image.open(_openable(path)) as img:
            return getattr(img, "n_frames", 1)
    except (OSError, ValueError):
        return 1


def to_png(src: Path, dst: Path) -> None:
    """Convert ``src`` (GAL or standard format) to PNG at ``dst``."""
    frames = frame_count(src)
    if frames > 1:
        raise MultiFrameImage(
            f"{src.name} holds {frames} animation frames, and this step can carry "
            "only one — converting it would replace the animation with a still."
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(_openable(src)) as img:
        img.load()
        img.save(dst)


def opaque_view(source: Path, destination: Path) -> Path:
    """``source``, or a copy of it composited onto a backdrop it can be read on."""
    with Image.open(_openable(source)) as picture:
        picture.load()
        rgba = picture.convert("RGBA")
    alpha = rgba.getchannel("A")
    lowest, highest = alpha.getextrema()
    if lowest == 255:
        return source
    visible = alpha.point(lambda opacity: 255 if opacity else 0)
    ink = ImageStat.Stat(rgba.convert("L"), visible).mean[0] if highest else 0
    shade = 255 if ink < 128 else 0
    backdrop = Image.new("RGBA", rgba.size, (shade, shade, shade, 255))
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(backdrop, rgba).convert("RGB").save(destination)
    return destination


def portable_ocr_model_dir(
    checkpoint: PpOcrCheckpoint = RECOGNIZER_CHECKPOINT,
) -> Path:
    """Model directory staged beside the executable in portable releases."""
    return _portable_root_dir() / "models" / checkpoint.dirname


def portable_ocr_runtime_dir() -> Path:
    """Runtime directory staged beside the portable executable."""
    return _portable_root_dir() / "ocr-runtime"


def portable_ocr_runtime() -> tuple[Path, Path] | None:
    """Return a verified CUDA sidecar when its executable and adapter are present."""
    runtime = portable_ocr_runtime_dir()
    python = runtime / "python" / "python.exe"
    runner = runtime / "ocr_runner.py"
    if runtime_is_complete(runtime):
        return python, runner
    return None


def portable_ocr_runtime_cached() -> bool:
    return portable_ocr_runtime() is not None


def ensure_portable_ocr_runtime(
    *,
    progress: RuntimeProgress,
    token: CancellationToken,
) -> bool:
    """Install a staged portable bootstrap on first use."""
    runtime = portable_ocr_runtime_dir()
    if runtime_is_complete(runtime):
        return False
    if not bootstrap_is_complete(runtime):
        if getattr(sys, "frozen", False) or globals().get("__compiled__") is not None:
            raise MissingDependency(f"OCR runtime bootstrap is unavailable: {runtime}")
        return False
    try:
        install_cuda_runtime(runtime, progress=progress, token=token)
    except (OSError, RuntimeError) as exc:
        raise MissingDependency(f"CUDA OCR runtime installation failed: {exc}") from exc
    return True


def portable_eraser_model() -> Path:
    """The eraser staged beside the executable in an offline release."""
    return _portable_root_dir() / "models" / render_mod.MODEL_NAME


def eraser_model_path() -> Path:
    """The staged eraser when the release carries one, else the user cache."""
    staged = portable_eraser_model()
    return staged if staged.is_file() else render_mod.model_path()


def eraser_model_cached() -> bool:
    return eraser_model_path().is_file()


def portable_renderer() -> tuple[Path, Path] | None:
    """The staged interpreter and render runner, when both are present."""
    sidecar = portable_ocr_runtime()
    if sidecar is None:
        return None
    python, _ = sidecar
    runtime = python.parents[1]
    runner = runtime / "render_runner.py"
    if runner.is_file() and (runtime / "render.py").is_file():
        return python, runner
    return None


def is_complete_ocr_snapshot(
    path: Path,
    checkpoint: PpOcrCheckpoint = RECOGNIZER_CHECKPOINT,
) -> bool:
    try:
        cached_files = {item.name for item in path.iterdir() if item.is_file()}
    except OSError:
        return False
    return checkpoint.files.issubset(cached_files) and checkpoint.weights.issubset(
        cached_files
    )


def _download_progress_bar(report: Callable[[int], None]) -> type:
    from tqdm.auto import tqdm

    class SilentDownloadProgress(tqdm):
        def display(self, msg=None, pos=None) -> None:
            del msg, pos

        def update(self, n=1):
            result = super().update(n)
            report(int(self.n))
            return result

    return SilentDownloadProgress


def _download_ocr_snapshot(
    hf_hub_download,
    progress: DownloadProgress,
    checkpoint: PpOcrCheckpoint,
) -> Path:
    last_percentage = -1
    progress_lock = threading.Lock()

    def report(completed: int) -> None:
        nonlocal last_percentage
        completed = max(0, min(completed, checkpoint.download_bytes))
        percentage = completed * 100 // checkpoint.download_bytes
        with progress_lock:
            if percentage == last_percentage and completed < checkpoint.download_bytes:
                return
            last_percentage = percentage
            progress(completed, checkpoint.download_bytes)

    report(0)
    completed = 0
    snapshot: Path | None = None
    for filename, size in checkpoint.manifest:
        offset = completed
        progress_bar = _download_progress_bar(
            lambda file_bytes, offset=offset: report(offset + file_bytes)
        )
        downloaded = Path(
            hf_hub_download(
                checkpoint.model_id,
                filename,
                revision=checkpoint.revision,
                local_files_only=False,
                tqdm_class=progress_bar,
            )
        )
        snapshot = downloaded.parent
        completed += size
        report(completed)
    if snapshot is None:
        raise MissingDependency("OCR model manifest is empty.")
    return snapshot


def ocr_model_snapshot(
    *,
    checkpoint: PpOcrCheckpoint = RECOGNIZER_CHECKPOINT,
    allow_download: bool = False,
    allow_portable: bool = True,
    download_progress: DownloadProgress | None = None,
) -> Path:
    """Resolve the pinned portable or Hugging Face model snapshot."""
    if allow_portable:
        portable = portable_ocr_model_dir(checkpoint)
        if is_complete_ocr_snapshot(portable, checkpoint):
            return portable
    try:
        import huggingface_hub
    except ImportError as exc:
        raise MissingDependency(
            "OCR runtime is unavailable. Reinstall Bokucho with the images extra."
        ) from exc
    try:
        if allow_download and download_progress is not None:
            snapshot = _download_ocr_snapshot(
                huggingface_hub.hf_hub_download,
                download_progress,
                checkpoint,
            )
        else:
            snapshot = Path(
                huggingface_hub.snapshot_download(
                    checkpoint.model_id,
                    revision=checkpoint.revision,
                    local_files_only=not allow_download,
                )
            )
    except JobCancelled:
        raise
    except Exception as exc:
        action = "download" if allow_download else "find"
        raise MissingDependency(
            f"Could not {action} the pinned OCR model. "
            "Use the portable release or approve the model download and retry."
        ) from exc
    if not is_complete_ocr_snapshot(snapshot, checkpoint):
        raise MissingDependency("OCR model snapshot is incomplete.")
    return snapshot


def ocr_model_cached(
    checkpoint: PpOcrCheckpoint = RECOGNIZER_CHECKPOINT,
) -> bool:
    """Return whether a fixed PP-OCR checkpoint is available offline."""
    try:
        ocr_model_snapshot(checkpoint=checkpoint, allow_download=False)
    except MissingDependency:
        return False
    return True


def _stop_sidecar(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        if process.poll() is None:
            process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
    finally:
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass


def _terminate_sidecar(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
    except OSError:
        pass


def _exit_code_suffix(process: subprocess.Popen[str]) -> str:
    try:
        returncode = process.poll()
    except OSError:
        returncode = None
    return f" (exit code {returncode})" if returncode is not None else ""


class _Sidecar:
    """A staged runtime's Python, answering each JSON line with one JSON line."""

    _label = ""
    _verb = ""
    _verbing = ""
    _environment: dict[str, str] = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    _lost: type[Exception] = MissingDependency
    _refused: type[Exception] = MissingDependency

    def __init__(self, python_executable: Path, runner_script: Path) -> None:
        self._python_executable = python_executable
        self._runner_script = runner_script
        self._process: subprocess.Popen[str] | None = None
        self._finalizer: weakref.finalize | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._process_lock = threading.Lock()
        self._cancelled = threading.Event()

    def sidecar_command(self) -> list[str]:
        """Everything the sidecar is told, in the order it is told it."""
        raise NotImplementedError

    def _start(self) -> subprocess.Popen[str]:
        if self._cancelled.is_set():
            raise JobCancelled()
        if self._closed:
            raise MissingDependency(f"{self._label} sidecar has already been closed.")
        if self._process is not None:
            return self._process
        process = self._spawn()
        finalizer = weakref.finalize(self, _stop_sidecar, process)
        with self._process_lock:
            cancelled = self._cancelled.is_set()
            if not cancelled:
                self._process = process
                self._finalizer = finalizer
        if cancelled:
            _terminate_sidecar(process)
            finalizer()
            raise JobCancelled()
        return process

    def _spawn(self) -> subprocess.Popen[str]:
        options: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": {**os.environ, **self._environment},
        }
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(self.sidecar_command(), **options)
        except OSError as exc:
            raise MissingDependency(
                f"{self._label} sidecar could not start. Reinstall the portable release."
            ) from exc
        if process.stdin is None or process.stdout is None:
            _stop_sidecar(process)
            raise MissingDependency(
                f"{self._label} sidecar could not open its communication channel."
            )
        return process

    def _request(self, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            process = self._start()
            response_line = self._exchange(process, json.dumps(payload, ensure_ascii=False))
            try:
                response = json.loads(response_line)
            except json.JSONDecodeError as exc:
                raise self._invalid() from exc
            if not isinstance(response, dict):
                raise self._invalid()
            if response.get("ok") is not True:
                error = response.get("error")
                detail = error.strip() if isinstance(error, str) else "unknown failure"
                raise self._refused(
                    f"{self._label} sidecar could not {self._verb} the image: {detail}"
                )
            return response

    def _exchange(self, process: subprocess.Popen[str], request: str) -> str:
        assert process.stdin is not None
        assert process.stdout is not None
        try:
            process.stdin.write(request + "\n")
            process.stdin.flush()
            response_line = process.stdout.readline()
        except (BrokenPipeError, OSError) as exc:
            raise self._gone(process, f"stopped while {self._verbing} an image") from exc
        if not response_line:
            raise self._gone(process, "exited without returning a result")
        return response_line

    def _gone(self, process: subprocess.Popen[str], what: str) -> Exception:
        exit_code = _exit_code_suffix(process)
        self.close()
        if self._cancelled.is_set():
            return JobCancelled()
        return self._lost(f"{self._label} sidecar {what}{exit_code}.")

    def _invalid(self, what: str = "response") -> Exception:
        self.close()
        return self._lost(f"{self._label} sidecar returned an invalid {what}.")

    def cancel(self) -> None:
        self._cancelled.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            _terminate_sidecar(process)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            finalizer = self._finalizer
            self._finalizer = None
            with self._process_lock:
                self._process = None
            if finalizer is not None:
                finalizer()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


_RUNTIME_REPORT_FIELDS = {
    "requested_precision": str,
    "weight_format": str,
    "compute_dtype": str,
    "quantized_linear_layers": int,
    "model_memory_bytes": int,
    "vram_budget_bytes": int,
}


class PortableOcrReader(_Sidecar):
    """Persistent JSON-lines client for the portable OCR Python runtime."""

    _label = "OCR"
    _verb = "recognize"
    _verbing = "recognizing"
    _environment = {
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_OFFLINE": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "TRANSFORMERS_OFFLINE": "1",
    }
    _lost = OcrRuntimeLost

    def __init__(
        self,
        python_executable: Path,
        runner_script: Path,
        model_dir: Path,
        batch_size: int | None = None,
        detection_threshold: float = 0.9,
        recognition_threshold: float = 0.9,
    ) -> None:
        super().__init__(python_executable, runner_script)
        self._model_dir = model_dir
        self._detection_threshold = detection_threshold
        self._recognition_threshold = recognition_threshold
        self.batch_size = batch_size or DEFAULT_BATCH_SIZE
        self.visual_floor_pixels = VISUAL_FLOOR_PIXELS
        self._runtime_report: dict[str, object] | None = None

    def sidecar_command(self) -> list[str]:
        return [
            str(self._python_executable),
            "-I",
            str(self._runner_script),
            "--model",
            str(self._model_dir),
            "--batch-size",
            str(self.batch_size),
            "--detection-threshold",
            str(self._detection_threshold),
            "--recognition-threshold",
            str(self._recognition_threshold),
        ]

    @property
    def runtime_report(self) -> dict[str, object]:
        if self._runtime_report is None:
            runtime = self._request({"action": "status"}).get("runtime")
            if not isinstance(runtime, dict) or any(
                not isinstance(runtime.get(key), value_type)
                for key, value_type in _RUNTIME_REPORT_FIELDS.items()
            ):
                raise self._invalid("runtime report")
            self._runtime_report = dict(runtime)
        return dict(self._runtime_report)

    def __call__(self, image_path: str | Path) -> str:
        text = self._request({"path": str(Path(image_path).resolve())}).get("text")
        if not isinstance(text, str):
            raise self._invalid()
        return text

    def read_many(self, image_paths: Sequence[str | Path]) -> list[str]:
        paths = [str(Path(path).resolve()) for path in image_paths]
        if not paths:
            return []
        return self._texts(self._request({"paths": paths}), len(paths))

    def detect_many(
        self, image_paths: Sequence[str | Path]
    ) -> list[list[list[int]]]:
        """Where the text sits on each picture, from a sidecar holding a detector."""
        paths = [str(Path(path).resolve()) for path in image_paths]
        if not paths:
            return []
        boxes = self._request({"action": "detect", "paths": paths}).get("boxes")
        if (
            not isinstance(boxes, list)
            or len(boxes) != len(paths)
            or not all(isinstance(located, list) for located in boxes)
        ):
            raise self._invalid()
        return boxes

    def read_located(
        self,
        image_paths: Sequence[str | Path],
        boxes: Sequence[Sequence[Sequence[int]]],
    ) -> list[str]:
        """What the located text says, from a sidecar holding a reader."""
        paths = [str(Path(path).resolve()) for path in image_paths]
        if not paths:
            return []
        response = self._request(
            {
                "action": "read",
                "paths": paths,
                "boxes": [[list(box) for box in located] for located in boxes],
            }
        )
        return self._texts(response, len(paths))

    def _texts(self, response: dict[str, object], count: int) -> list[str]:
        texts = response.get("texts")
        if (
            not isinstance(texts, list)
            or len(texts) != count
            or not all(isinstance(text, str) for text in texts)
        ):
            raise self._invalid()
        return texts


class PortableRenderer(_Sidecar):
    """Persistent JSON-lines client for the staged erase-and-typeset runner."""

    _label = "Render"
    _verb = "typeset"
    _verbing = "typesetting"
    _refused = ValueError

    def __init__(
        self,
        python_executable: Path,
        runner_script: Path,
        model_path: Path,
        font: str,
    ) -> None:
        super().__init__(python_executable, runner_script)
        self._model_path = model_path
        self._font = font

    def sidecar_command(self) -> list[str]:
        return [
            str(self._python_executable),
            "-I",
            str(self._runner_script),
            "--model",
            str(self._model_path),
        ]

    def render(
        self, source: Path, manifest: dict, target: str, destination: Path, size: int | None = None
    ) -> dict:
        """Erase and typeset one picture, at ``size`` if given; returns status, reason, size."""
        response = self._request(
            {
                "source": str(source),
                "manifest": manifest,
                "target": target,
                "font": self._font,
                "destination": str(destination),
                "size": size,
            }
        )
        return {
            "image": str(response.get("image", "")),
            "status": str(response.get("status", "")),
            "reason": str(response.get("reason", "")),
            "size": response.get("size"),
        }


class InProcessRenderer:
    """The renderer in this interpreter, for a source checkout with torch."""

    def __init__(self, model_path: Path, font: str) -> None:
        self._model_path = model_path
        self._font = font
        self._inpaint = None

    def _inpaint_fn(self):
        if self._inpaint is None:
            from bokucho import render

            self._inpaint = render.make_inpaint_fn(self._model_path)
        return self._inpaint

    def render(
        self, source: Path, manifest: dict, target: str, destination: Path, size: int | None = None
    ) -> dict:
        """Erase and typeset one picture, at ``size`` if given; returns status, reason, size."""
        from bokucho import render

        result = render.render_image(
            Path(source),
            dict(manifest),
            str(target),
            self._font,
            self._inpaint_fn(),
            Path(destination),
            size,
        )
        return {
            "image": result.image,
            "status": result.status,
            "reason": result.reason,
            "size": result.size,
        }

    def cancel(self) -> None:
        """Nothing to interrupt: a picture already being erased runs to its end."""

    def close(self) -> None:
        self._inpaint = None


def get_renderer(model_path: Path, font: str) -> PortableRenderer | InProcessRenderer:
    """A renderer backed by the staged runtime, or by this interpreter's torch."""
    runtime = portable_renderer()
    if runtime is None:
        try:
            import torch  # noqa: F401
        except ImportError as exc:
            raise MissingDependency(
                "The render runtime is unavailable. Reinstall the portable release."
            ) from exc
        return InProcessRenderer(model_path, font)
    python, runner = runtime
    return PortableRenderer(python, runner, model_path, font)


def get_ocr(
    *,
    allow_download: bool = False,
    download_progress: DownloadProgress | None = None,
    batch_size: int | None = None,
    reads_crops: bool = False,
    locating: bool = False,
    detection_threshold: float = 0.9,
    recognition_threshold: float = 0.9,
):
    """Return one fixed half of the PP-OCR detector/recognizer pipeline."""
    if locating == reads_crops:
        raise ValueError("OCR must either locate text or read located text.")
    checkpoint = DETECTOR_CHECKPOINT if locating else RECOGNIZER_CHECKPOINT
    runtime = portable_ocr_runtime()
    if runtime is None:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError as exc:
            raise MissingDependency(
                "OCR runtime is unavailable. Reinstall Bokucho with the images extra."
            ) from exc
    snapshot = ocr_model_snapshot(
        checkpoint=checkpoint,
        allow_download=allow_download,
        download_progress=download_progress,
    )
    if runtime is not None:
        python, runner = runtime
        return PortableOcrReader(
            python,
            runner,
            snapshot,
            batch_size,
            detection_threshold,
            recognition_threshold,
        )
    return _in_process_half(
        snapshot, batch_size, locating, detection_threshold, recognition_threshold
    )


def _in_process_half(
    snapshot: Path,
    batch_size: int | None,
    locating: bool,
    detection_threshold: float,
    recognition_threshold: float,
):
    """One OCR half run in this interpreter, for a source checkout with torch."""
    try:
        reader = _in_process_reader(snapshot, batch_size)
        if locating:
            return _runner_module().LocatingReader(reader, detection_threshold)
        return _runner_module().ReadingReader(
            reader,
            batch_size or DEFAULT_BATCH_SIZE,
            recognition_threshold,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise MissingDependency(
            "OCR runtime could not load the pinned model. Reinstall the portable release."
        ) from exc


def _in_process_reader(snapshot: Path, batch_size: int | None):
    """The adapter itself, in this interpreter, for a source checkout with torch."""
    return _pp_ocr_reader(
        snapshot,
        batch_size=batch_size or DEFAULT_BATCH_SIZE,
    )


def _runner_module():
    """The runner, loaded by path rather than imported."""
    runner = portable_ocr_runtime_dir() / "ocr_runner.py"
    if not runner.is_file():
        runner = Path(__file__).resolve().parents[2] / "tools" / "ocr_runner.py"
    spec = importlib.util.spec_from_file_location("bokucho_ocr_runner_local", runner)
    if spec is None or spec.loader is None:
        raise MissingDependency(f"The OCR runner is unavailable at {runner}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
