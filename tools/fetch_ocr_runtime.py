"""Stage the OCR sidecar bootstrap into a portable release."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bokucho.images import (  # noqa: E402 - needs the path above
    OCR_MODEL_DIRNAME,
    is_complete_ocr_snapshot,
)
from bokucho.infrastructure.ocr_runtime import (  # noqa: E402
    OCR_ACCELERATE_VERSION,
    OCR_BOOTSTRAP_METADATA_NAME,
    OCR_PIP_VERSION,
    OCR_RUNTIME_METADATA_NAME,
    OCR_TORCH_BACKEND,
    OCR_TRANSFORMERS_VERSION,
    bootstrap_is_complete,
    expected_bootstrap_metadata,
    runtime_is_complete,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_ocr_model import resolve_release_dir  # noqa: E402 - same guard, one place

PORTABLE_DIR = ROOT / "dist" / "Bokucho-Windows-x64"
RUNNER_SOURCE = ROOT / "tools" / "ocr_runner.py"
PP_OCR_ADAPTER_SOURCE = ROOT / "src" / "bokucho" / "infrastructure" / "pp_ocr.py"
RENDER_RUNNER_SOURCE = ROOT / "tools" / "render_runner.py"
RENDER_SOURCE = ROOT / "src" / "bokucho" / "render.py"
GAL_SOURCE = ROOT / "src" / "bokucho" / "gal.py"
RPGMV_ASSETS_SOURCE = ROOT / "src" / "bokucho" / "engines" / "rpgmv_assets.py"

PYTHON_VERSION = "3.11.9"
PYTHON_ZIP_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
PYTHON_ZIP_SHA256 = "009d6bf7e3b2ddca3d784fa09f90fe54336d5b60f0e0f305c37f400bf83cfd3b"
EMBEDDED_PYTHON = (
    "--python-version",
    PYTHON_VERSION,
    "--python-platform",
    "x86_64-pc-windows-msvc",
)

TORCH_BACKEND = OCR_TORCH_BACKEND
RUNTIME_METADATA_NAME = OCR_RUNTIME_METADATA_NAME
BOOTSTRAP_METADATA_NAME = OCR_BOOTSTRAP_METADATA_NAME
SHARED_TRANSFORMERS_VERSION = OCR_TRANSFORMERS_VERSION

OCR_BOOTSTRAP_PACKAGES = (
    f"transformers=={SHARED_TRANSFORMERS_VERSION}",
    "fonttools>=4.0",
    "filelock",
    "fsspec",
    "jinja2",
    "networkx",
    "numpy",
    "packaging",
    "pillow",
    "psutil",
    "pyyaml",
    "safetensors",
    "setuptools",
    "sympy",
    "typing-extensions",
)
OCR_BOOTSTRAP_NO_DEPS_PACKAGES = (
    f"accelerate=={OCR_ACCELERATE_VERSION}",
    f"pip=={OCR_PIP_VERSION}",
)

Fetcher = Callable[[str, str], Path]
Installer = Callable[[Path], None]
Verifier = Callable[[Path], dict[str, str]]


def _download(url: str, sha256: str) -> Path:
    """Fetch ``url`` to a temporary file, keeping a good copy for the next run."""
    destination = Path(tempfile.gettempdir()) / f"bokucho-{Path(url).name}"
    if not destination.is_file() or _digest(destination) != sha256:
        with urllib.request.urlopen(url) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    actual = _digest(destination)
    if actual != sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"{url} hash mismatch: {actual}")
    return destination


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _uv_install(site_packages: Path) -> None:
    """Install only the packages needed before the first CUDA download."""
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(site_packages),
            *EMBEDDED_PYTHON,
            *OCR_BOOTSTRAP_PACKAGES,
        ],
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(site_packages),
            *EMBEDDED_PYTHON,
            "--no-deps",
            *OCR_BOOTSTRAP_NO_DEPS_PACKAGES,
        ],
        check=True,
    )


def _verify_bootstrap(python_dir: Path) -> dict[str, str]:
    process = subprocess.run(
        [
            str(python_dir / "python.exe"),
            "-I",
            "-c",
            (
                "import numpy, PIL.Image, psutil, safetensors, yaml, fontTools.ttLib; "
                "import json; from importlib.metadata import version; "
                "print(json.dumps({'accelerate': version('accelerate'), "
                "'pip': version('pip'), "
                "'transformers': version('transformers')}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    metadata = json.loads(process.stdout.splitlines()[-1])
    accelerate_version = str(metadata.get("accelerate", ""))
    pip_version = str(metadata.get("pip", ""))
    transformers_version = str(metadata.get("transformers", ""))
    if accelerate_version != OCR_ACCELERATE_VERSION:
        raise RuntimeError(
            "The OCR bootstrap has the wrong Accelerate version: "
            f"accelerate={accelerate_version or 'missing'}"
        )
    if pip_version != OCR_PIP_VERSION:
        raise RuntimeError(
            f"The OCR bootstrap has the wrong pip version: pip={pip_version or 'missing'}"
        )
    if transformers_version != SHARED_TRANSFORMERS_VERSION:
        raise RuntimeError(
            "The staged OCR runtime has the wrong shared Transformers version: "
            f"transformers={transformers_version or 'missing'}"
        )
    return {
        "accelerate": accelerate_version,
        "pip": pip_version,
        "transformers": transformers_version,
    }


def _enable_site_packages(python_dir: Path) -> None:
    """Put ``Lib\\site-packages`` on the embeddable interpreter's path."""
    path_files = list(python_dir.glob("python*._pth"))
    if not path_files:
        raise RuntimeError(f"No ._pth file in the embeddable distribution at {python_dir}")
    path_file = path_files[0]
    lines = path_file.read_text(encoding="utf-8").splitlines()
    lines = ["import site" if line.strip() == "#import site" else line for line in lines]
    if "Lib\\site-packages" not in lines:
        lines.insert(0, "Lib\\site-packages")
    if "import site" not in lines:
        lines.append("import site")
    path_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage_sources(staging: Path) -> None:
    """Copy every file the sidecar runs from into ``staging``."""
    shutil.copy2(RUNNER_SOURCE, staging / "ocr_runner.py")
    shutil.copy2(PP_OCR_ADAPTER_SOURCE, staging / "pp_ocr.py")
    shutil.copy2(RENDER_RUNNER_SOURCE, staging / "render_runner.py")
    shutil.copy2(RENDER_SOURCE, staging / "render.py")
    shutil.copy2(GAL_SOURCE, staging / "gal.py")
    shutil.copy2(RPGMV_ASSETS_SOURCE, staging / "rpgmv_assets.py")
    sample_header = b"P6\n64 32\n255\n"
    (staging / "example.ppm").write_bytes(sample_header + b"\xff" * (64 * 32 * 3))


def stage_runtime(
    release_dir: Path,
    fetch: Fetcher = _download,
    install: Installer = _uv_install,
    verify: Verifier = _verify_bootstrap,
) -> Path:
    """Build ``<release_dir>/ocr-runtime`` and return it."""
    runtime = release_dir / "ocr-runtime"
    if runtime_is_complete(runtime) or bootstrap_is_complete(runtime):
        return runtime

    staging = runtime.with_name(runtime.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    python_dir = staging / "python"
    python_dir.mkdir(parents=True)

    archive = fetch(PYTHON_ZIP_URL, PYTHON_ZIP_SHA256)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(python_dir)
    _enable_site_packages(python_dir)
    install(python_dir / "Lib" / "site-packages")
    runtime_metadata = verify(python_dir)
    runtime_metadata.update(expected_bootstrap_metadata())

    stage_sources(staging)
    (staging / BOOTSTRAP_METADATA_NAME).write_text(
        json.dumps(runtime_metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if runtime.exists():
        shutil.rmtree(runtime)
    staging.rename(runtime)
    return runtime


def smoke_test(release_dir: Path) -> str:
    """Recognize the packaged sample through the staged sidecar."""
    runtime = release_dir / "ocr-runtime"
    model = release_dir / "models" / OCR_MODEL_DIRNAME
    sample = runtime / "example.ppm"
    if not is_complete_ocr_snapshot(model):
        raise RuntimeError(f"No staged model at {model}; run tools/fetch_ocr_model.py first.")
    if not sample.is_file():
        raise RuntimeError(f"No sample image at {sample}.")
    process = subprocess.run(
        [
            str(runtime / "python" / "python.exe"),
            "-I",
            str(runtime / "ocr_runner.py"),
            "--model",
            str(model),
        ],
        input=json.dumps({"path": str(sample)}) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    first = process.stdout.splitlines()[0] if process.stdout else ""
    response = json.loads(first) if first else {}
    if response.get("ok") is not True:
        raise RuntimeError(f"Sidecar smoke test failed: {response or process.stderr[-500:]}")
    return str(response["text"])


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=PORTABLE_DIR,
        help=f"Portable release directory (default: {PORTABLE_DIR}).",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Recognize the packaged sample using a separately staged model.",
    )
    arguments = parser.parse_args(argv)
    try:
        release_dir = resolve_release_dir(arguments.release_dir)
    except ValueError as error:
        parser.error(str(error))
    runtime = stage_runtime(release_dir)
    print(f"Staged Python {PYTHON_VERSION} + OCR bootstrap -> {runtime}")
    if arguments.smoke_test:
        print(f"Sidecar read the packaged sample as: {smoke_test(release_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
