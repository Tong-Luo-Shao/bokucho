"""First-use installation of the portable CUDA OCR runtime."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bokucho.application.contracts import CancellationToken

OCR_TORCH_BACKEND = "cu130"
OCR_TORCH_VERSION = "2.13.0"
OCR_ACCELERATE_VERSION = "1.14.0"
OCR_PIP_VERSION = "26.2"
OCR_TRANSFORMERS_VERSION = "5.14.1"
OCR_BOOTSTRAP_METADATA_NAME = "runtime-bootstrap.json"
OCR_RUNTIME_METADATA_NAME = "runtime.json"
OCR_RUNTIME_FILES = (
    "ocr_runner.py",
    "pp_ocr.py",
    "render_runner.py",
    "render.py",
    "gal.py",
    "rpgmv_assets.py",
    "example.ppm",
)


@dataclass(frozen=True, slots=True)
class RuntimeWheel:
    name: str
    filename: str
    url: str
    sha256: str
    size: int


CUDA_RUNTIME_WHEELS = (
    RuntimeWheel(
        name="torch",
        filename="torch-2.13.0+cu130-cp311-cp311-win_amd64.whl",
        url=(
            "https://download-r2.pytorch.org/whl/cu130/"
            "torch-2.13.0%2Bcu130-cp311-cp311-win_amd64.whl"
        ),
        sha256="45e97bd9bc0416f4f4190b5098c55119a389fa5a7c8bbf2639f08f1d04e0a0dc",
        size=1_915_488_103,
    ),
    RuntimeWheel(
        name="torchvision",
        filename="torchvision-0.28.0+cu130-cp311-cp311-win_amd64.whl",
        url=(
            "https://download-r2.pytorch.org/whl/cu130/"
            "torchvision-0.28.0%2Bcu130-cp311-cp311-win_amd64.whl"
        ),
        sha256="0c6921bb5e3e58d926f80ff894739c8b9b0e72fed59a062e19c934f13dfd53ea",
        size=8_819_700,
    ),
    RuntimeWheel(
        name="opencv",
        filename="opencv_python_headless-5.0.0.93-cp37-abi3-win_amd64.whl",
        url=(
            "https://files.pythonhosted.org/packages/b8/88/"
            "763b967f7efd7226b82c9fae16d560cba049b1f0c036647e65c610fd636e/"
            "opencv_python_headless-5.0.0.93-cp37-abi3-win_amd64.whl"
        ),
        sha256="829717b6a95554f273e49e357cee3b3a2a26b6f4842fbc1bed2b45bdd8f87e0e",
        size=43_825_962,
    ),
)
CUDA_RUNTIME_DOWNLOAD_BYTES = sum(wheel.size for wheel in CUDA_RUNTIME_WHEELS)

RuntimeProgress = Callable[[str, int, int], None]
WheelProgress = Callable[[int, int], None]
WheelDownloader = Callable[
    [RuntimeWheel, Path, WheelProgress, CancellationToken],
    Path,
]
WheelInstaller = Callable[[Path, tuple[Path, ...], CancellationToken], None]
RuntimeVerifier = Callable[[Path], dict[str, str]]

_INSTALL_LOCK = threading.Lock()
_VERIFIED_RUNTIME = {
    "torch": f"{OCR_TORCH_VERSION}+{OCR_TORCH_BACKEND}",
    "cuda": "13.0",
    "transformers": OCR_TRANSFORMERS_VERSION,
}


def expected_bootstrap_metadata() -> dict[str, object]:
    return {
        "accelerate": OCR_ACCELERATE_VERSION,
        "cuda_runtime_download_bytes": CUDA_RUNTIME_DOWNLOAD_BYTES,
        "pip": OCR_PIP_VERSION,
        "torch_backend": OCR_TORCH_BACKEND,
        "transformers": OCR_TRANSFORMERS_VERSION,
    }


def expected_runtime_contract() -> dict[str, object]:
    return {
        "torch_backend": OCR_TORCH_BACKEND,
    }


def _metadata(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _has_runtime_files(runtime: Path) -> bool:
    return (runtime / "python" / "python.exe").is_file() and all(
        (runtime / name).is_file() for name in OCR_RUNTIME_FILES
    )


def bootstrap_is_complete(runtime: Path) -> bool:
    metadata = _metadata(runtime / OCR_BOOTSTRAP_METADATA_NAME)
    expected = expected_bootstrap_metadata()
    return (
        _has_runtime_files(runtime)
        and metadata is not None
        and all(metadata.get(key) == value for key, value in expected.items())
    )


def runtime_is_complete(runtime: Path) -> bool:
    metadata = _metadata(runtime / OCR_RUNTIME_METADATA_NAME)
    packages = runtime / "python" / "Lib" / "site-packages"
    contract = expected_runtime_contract()
    return (
        _has_runtime_files(runtime)
        and metadata is not None
        and all(
            metadata.get(key) == value
            for key, value in {**contract, **_VERIFIED_RUNTIME}.items()
        )
        and all((packages / name).is_dir() for name in ("torch", "torchvision", "cv2"))
    )


def _download_wheel(
    wheel: RuntimeWheel,
    destination: Path,
    progress: WheelProgress,
    token: CancellationToken,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / wheel.filename
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(
        wheel.url,
        headers={"Accept-Encoding": "identity", "User-Agent": "Bokucho/1"},
    )
    token.raise_if_cancelled()
    response = urllib.request.urlopen(request, timeout=15)
    token.add_cancel_callback(response.close)
    digest = hashlib.sha256()
    received = 0
    try:
        declared_size = response.headers.get("Content-Length")
        if declared_size is not None and int(declared_size) != wheel.size:
            raise RuntimeError(
                f"{wheel.name} download size changed: {declared_size} != {wheel.size}"
            )
        with response, partial.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                token.raise_if_cancelled()
                handle.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                progress(received, wheel.size)
    except (OSError, ValueError):
        token.raise_if_cancelled()
        raise
    token.raise_if_cancelled()
    actual_hash = digest.hexdigest()
    if received != wheel.size or actual_hash != wheel.sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"{wheel.name} integrity check failed: size={received}, sha256={actual_hash}"
        )
    partial.replace(target)
    return target


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()


def _run_cancellable(command: list[str], token: CancellationToken) -> str:
    startupinfo = None
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        startupinfo=startupinfo,
    )
    token.add_cancel_callback(lambda: _terminate(process))
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            token.raise_if_cancelled()
    token.raise_if_cancelled()
    if process.returncode:
        detail = stderr.strip()[-1000:] or f"exit code {process.returncode}"
        raise RuntimeError(f"Runtime installer failed: {detail}")
    return stdout


def _pip_install_wheels(
    python_dir: Path,
    wheels: tuple[Path, ...],
    token: CancellationToken,
) -> None:
    _run_cancellable(
        [
            str(python_dir / "python.exe"),
            "-I",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-input",
            "--no-deps",
            "--upgrade",
            "--target",
            str(python_dir / "Lib" / "site-packages"),
            *(str(wheel) for wheel in wheels),
        ],
        token,
    )


def _verify_cuda_runtime(python_dir: Path, token: CancellationToken) -> dict[str, str]:
    shared_output = _run_cancellable(
        [
            str(python_dir / "python.exe"),
            "-I",
            "-c",
            (
                "import json, cv2, torch, transformers; "
                "print(json.dumps({'torch': torch.__version__, "
                "'cuda': torch.version.cuda or '', "
                "'transformers': transformers.__version__}))"
            ),
        ],
        token,
    )
    metadata = json.loads(shared_output.splitlines()[-1])
    if not isinstance(metadata, dict) or any(
        str(metadata.get(key, "")) != value for key, value in _VERIFIED_RUNTIME.items()
    ):
        raise RuntimeError(f"CUDA runtime verification failed: {metadata}")
    return {str(key): str(value) for key, value in metadata.items()}


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _promote_runtime(runtime: Path, staging: Path) -> None:
    backup = runtime.with_name(runtime.name + ".bootstrap")
    _remove_tree(backup)
    runtime.rename(backup)
    try:
        staging.rename(runtime)
    except BaseException:
        backup.rename(runtime)
        raise
    _remove_tree(backup)


def _download_wheels(
    downloads: Path,
    progress: RuntimeProgress,
    token: CancellationToken,
    downloader: WheelDownloader,
) -> tuple[Path, ...]:
    progress("download", 0, CUDA_RUNTIME_DOWNLOAD_BYTES)
    wheel_paths: list[Path] = []
    downloaded = 0
    for wheel in CUDA_RUNTIME_WHEELS:
        token.raise_if_cancelled()

        def report(current: int, total: int, *, offset: int = downloaded) -> None:
            del total
            progress("download", offset + current, CUDA_RUNTIME_DOWNLOAD_BYTES)

        wheel_paths.append(downloader(wheel, downloads, report, token))
        downloaded += wheel.size
    progress("download", CUDA_RUNTIME_DOWNLOAD_BYTES, CUDA_RUNTIME_DOWNLOAD_BYTES)
    return tuple(wheel_paths)


def _record_runtime(
    staging: Path, token: CancellationToken, verifier: RuntimeVerifier | None
) -> None:
    verified = (
        verifier(staging / "python")
        if verifier is not None
        else _verify_cuda_runtime(staging / "python", token)
    )
    verified.update(expected_runtime_contract())
    (staging / OCR_RUNTIME_METADATA_NAME).write_text(
        json.dumps(verified, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not runtime_is_complete(staging):
        raise RuntimeError("Downloaded CUDA OCR runtime failed verification")


def install_cuda_runtime(
    runtime: Path,
    *,
    progress: RuntimeProgress,
    token: CancellationToken,
    downloader: WheelDownloader = _download_wheel,
    installer: WheelInstaller = _pip_install_wheels,
    verifier: RuntimeVerifier | None = None,
) -> Path:
    """Install the deferred CUDA wheels and atomically promote the verified tree."""
    with _INSTALL_LOCK:
        if runtime_is_complete(runtime):
            return runtime
        if not bootstrap_is_complete(runtime):
            raise RuntimeError(f"OCR runtime bootstrap is incomplete: {runtime}")

        staging = runtime.with_name(runtime.name + ".partial")
        _remove_tree(staging)
        shutil.copytree(runtime, staging)
        downloads = staging / ".downloads"
        try:
            downloads.mkdir()
            wheel_paths = _download_wheels(downloads, progress, token, downloader)
            progress("install", 0, 1)
            installer(staging / "python", wheel_paths, token)
            _remove_tree(downloads)
            token.raise_if_cancelled()
            _record_runtime(staging, token, verifier)
            progress("install", 1, 1)
            token.raise_if_cancelled()
            _promote_runtime(runtime, staging)
        except BaseException:
            _remove_tree(staging)
            backup = runtime.with_name(runtime.name + ".bootstrap")
            if backup.exists() and not runtime.exists():
                backup.rename(runtime)
            raise
        return runtime
