"""Erase and typeset pictures in the staged runtime, one JSON line each."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from typing import TextIO

RendererFactory = Callable[[Path], object]


def _configure_standard_streams() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_render_module():
    path = Path(__file__).resolve().with_name("render.py")
    staged = str(path.parent)
    if staged not in sys.path:
        sys.path.insert(0, staged)
    spec = importlib.util.spec_from_file_location("bokucho_render", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the renderer at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _lama_factory(model_path: Path):
    module = _load_render_module()
    return module, module.make_inpaint_fn(model_path)


def _write_response(output_stream: TextIO, payload: dict[str, object]) -> None:
    output_stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    output_stream.flush()


def serve(
    model_path: Path,
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    renderer_factory: RendererFactory = _lama_factory,
) -> None:
    loaded: tuple[object, object] | None = None
    for line in input_stream:
        try:
            request = json.loads(line)
            with redirect_stdout(sys.stderr):
                if loaded is None:
                    loaded = renderer_factory(model_path)
                module, inpaint_fn = loaded
                result = module.render_image(
                    Path(request["source"]),
                    dict(request["manifest"]),
                    str(request["target"]),
                    str(request["font"]),
                    inpaint_fn,
                    Path(request["destination"]),
                    request.get("size"),
                )
                response = {
                    "ok": True,
                    "image": result.image,
                    "status": result.status,
                    "reason": result.reason,
                    "size": result.size,
                }
        except Exception as exc:  # noqa: BLE001 - failures cross the process boundary
            message = str(exc).strip() or type(exc).__name__
            _write_response(output_stream, {"ok": False, "error": message})
        else:
            _write_response(output_stream, response)


def main() -> int:
    _configure_standard_streams()
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    args = parser.parse_args()
    serve(args.model, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
