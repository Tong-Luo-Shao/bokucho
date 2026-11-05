"""Job registry -- job name -> factory(state) -> callable(sink, token)."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from bokucho import images as images_mod
from bokucho import render as render_mod
from bokucho.application.assets import (
    PAINTED_BY_HAND,
    extract_images,
    load_inventory,
    render_pictures,
    typeset_font,
)
from bokucho.application.contracts import CancellationToken, ProgressEvent, ProgressSink
from bokucho.application.errors import InvalidProject, ValidationFailure
from bokucho.application.fonts import patch_game_font
from bokucho.application.projects import (
    extract_text,
    inject_translations,
    resolve_game_path,
)
from bokucho.application.quality import fix_brackets, fix_values
from bokucho.application.release import (
    build_patch,
    build_proxy_map,
    bundled_uif,
    patcher_stub,
    release_patcher,
)
from bokucho.application.terms import build_glossary
from bokucho.application.text import recheck_drafts, transform_project, translate_project
from bokucho.infrastructure.credentials import (
    default_credential_store,
    shared_provider_credentials,
)
from bokucho.infrastructure.factories import (
    make_engine_adapter,
    make_image_codec,
    make_prompt_token_counter,
    make_transform_pipeline,
    make_translation_lanes,
)
from bokucho.infrastructure.pp_ocr import DETECTOR_CHECKPOINT, RECOGNIZER_CHECKPOINT
from bokucho.project import Project
from bokucho.translators import provider_default_base_url
from bokucho.webapp.state import AppState, value_analysis_key

_log = logging.getLogger(__name__)

Operation = Callable[[ProgressSink, CancellationToken], object]


def _reloading(state: AppState, run: Operation) -> Operation:
    """``run``, refreshing the cached project however it ends."""

    def reload_after(sink: ProgressSink, token: CancellationToken) -> object:
        try:
            return run(sink, token)
        finally:
            state.reload()

    return reload_after


def translator_secrets(project: Project) -> tuple[str, ...]:
    """Resolve stored API keys for the project's configured translator provider."""
    interface = project.config.translator.selected_interface()
    endpoint = interface.base_url or provider_default_base_url(interface.provider)
    return shared_provider_credentials(
        default_credential_store(),
        interface.provider,
        endpoint,
        interface.id,
    )


def _build_patch(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        result = build_patch(
            project,
            adapter_factory=make_engine_adapter,
            uif=bundled_uif(),
        )
        return {"patcher": str(result.executable), "files": list(result.files)}

    return _reloading(state, run)


def _release(state: AppState) -> Operation:
    """Proxy map -> QA -> the patch executable, published as one staged artifact."""

    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        built = release_patcher(
            project,
            make_engine_adapter,
            project.root / "dist",
            patcher_stub(),
            sink,
            token,
            uif=bundled_uif(),
        )
        return {
            "artifact_dir": str(built.artifact_dir),
            "patcher": str(built.executable),
            "translated": built.translated,
            "total": built.total,
            "files": built.files,
            "proxy_total": built.proxy.total,
            "proxy_added": built.proxy.added,
            "proxy_repaired": built.proxy.repaired,
            "qa_issues": len(built.qa.issues),
        }

    return _reloading(state, run)


def _proxy_map(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        result = build_proxy_map(state.project)
        return {
            "total": result.total,
            "added": result.added,
            "repaired": result.repaired,
            "path": str(result.path),
        }

    return run


def _extract(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        result = extract_text(project, make_engine_adapter(project), sink, token)
        return {
            "units": result.units,
            "files": result.files,
            "strings_dir": str(result.strings_dir),
        }

    return _reloading(state, run)


def _patch_font(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        result = patch_game_font(state.project, make_engine_adapter, sink, token)
        return {
            "font": str(result.font),
            "drawn": result.drawn,
            "widened": result.widened,
            "undrawable": result.undrawable,
            "scripts": result.scripts,
            "slots": result.slots,
            "archive": str(result.archive) if result.archive else "",
        }

    return run


def _inject(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        result = inject_translations(
            project, make_engine_adapter(project), project.root / "out", sink, token
        )
        return {
            "translated": result.translated,
            "files": result.files,
            "total": result.total,
            "out_dir": str(result.out_dir),
        }

    return run


class _ShowingSavedFiles:
    """Progress that reads each file a run saves back into the GUI's state."""

    def __init__(self, sink: ProgressSink, state: AppState) -> None:
        self._sink = sink
        self._state = state

    def publish(self, event: ProgressEvent) -> None:
        if event.message_key == "translate.batch.saved" and event.detail:
            self._state.refresh_file(event.detail)
        self._sink.publish(event)


def _translate(state: AppState, redo: bool = False) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        result = translate_project(
            project,
            lanes=make_translation_lanes(project, translator_secrets(project)),
            sink=_ShowingSavedFiles(sink, state),
            token=token,
            token_counter=make_prompt_token_counter(project),
            redo=redo,
        )
        return asdict(result)

    return _reloading(state, run)


def _glossary(state: AppState) -> Operation:
    """Settle the script's proper nouns before anything is translated."""

    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        result = build_glossary(
            project,
            lanes=make_translation_lanes(project, translator_secrets(project)),
            sink=sink,
            token=token,
            token_counter=make_prompt_token_counter(project),
        )
        return {
            "examined": result.examined,
            "proposed": result.proposed,
            "added": result.added,
            "kept": result.kept,
        }

    return _reloading(state, run)


def _transform(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        result = transform_project(project, make_transform_pipeline(project), sink, token)
        return {"changed": result.changed, "transforms": result.transforms}

    return _reloading(state, run)


def _recheck(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        return asdict(recheck_drafts(state.project, sink, token))

    return _reloading(state, run)


def _fix_values(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        adapter = make_engine_adapter(project)
        values = state.analysed_values()
        if values is None:
            values = adapter.analyse_values(resolve_game_path(project), token)
            state.value_analysis = (value_analysis_key(project), values)
        return asdict(fix_values(project, lambda units: adapter.assemble_values(values, units)))

    return _reloading(state, run)


def _fix_brackets(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        return asdict(fix_brackets(state.project, token))

    return _reloading(state, run)


def _announce_runtime(runtime: dict, sink: ProgressSink) -> None:
    """Say what was loaded, once per model a scan opens."""
    _log.info(
        "OCR runtime verified: %s %s / %s weights / compute %s / "
        "%d quantized layers / %.2f GB / budget %.2f GB",
        runtime["model_id"],
        runtime["requested_precision"],
        runtime["weight_format"],
        runtime["compute_dtype"],
        runtime["quantized_linear_layers"],
        int(runtime["model_memory_bytes"]) / 1_000_000_000,
        int(runtime.get("vram_budget_bytes", 0)) / 1_000_000_000,
    )
    if runtime.get("detector_memory_bytes"):
        _log.info(
            "OCR detector loaded: %s / %.2f GB",
            runtime.get("detector_precision", ""),
            int(runtime["detector_memory_bytes"]) / 1_000_000_000,
        )
    sink.publish(
        ProgressEvent(
            "ocr-runtime-ready",
            1,
            1,
            "OCR runtime verified",
            detail=(
                f"{runtime['requested_precision']} / {runtime['weight_format']} / "
                f"compute {runtime['compute_dtype']} / "
                f"{runtime['quantized_linear_layers']} quantized layers / "
                f"{int(runtime['model_memory_bytes']) / 1_000_000_000:.2f} GB"
            ),
        )
    )


def _scan_summary(result) -> dict:
    return {
        "total": result.total,
        "failed": result.failed,
        "added": result.added,
        "inventory_path": str(result.inventory_path),
    }


def _images_extract(state: AppState) -> Operation:
    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        return _scan_summary(extract_images(state.project, sink=sink, token=token))

    return _reloading(state, run)


def _ensure_ocr_runtime(sink: ProgressSink, token: CancellationToken) -> None:
    def report(stage: str, completed: int, total: int) -> None:
        token.raise_if_cancelled()
        downloading = stage == "download"
        sink.publish(
            ProgressEvent(
                "ocr-runtime-download" if downloading else "ocr-runtime-install",
                completed,
                total,
                (
                    "Downloading CUDA OCR runtime"
                    if downloading
                    else "Installing CUDA OCR runtime"
                ),
                detail=(
                    f"{completed / 1_000_000_000:.2f} GB / "
                    f"{total / 1_000_000_000:.2f} GB"
                    if downloading
                    else None
                ),
            )
        )

    images_mod.ensure_portable_ocr_runtime(progress=report, token=token)


def _ocr_options(
    state: AppState,
    sink: ProgressSink,
    token: CancellationToken,
    detection_threshold: float | None,
) -> dict:
    """What both OCR halves are opened with: the saved settings, and a download report."""

    def report_download(completed: int, total: int) -> None:
        token.raise_if_cancelled()
        message = "Loading OCR model" if completed >= total else "Downloading OCR model"
        sink.publish(
            ProgressEvent(
                "ocr-model-download",
                completed,
                total,
                message,
                detail=(
                    f"{completed / 1_000_000_000:.2f} GB / "
                    f"{total / 1_000_000_000:.2f} GB"
                ),
            )
        )

    settings = state.settings.load()
    options = {
        "allow_download": True,
        "download_progress": report_download,
        "detection_threshold": (
            settings.ocr_detection_threshold
            if detection_threshold is None
            else detection_threshold
        ),
        "recognition_threshold": settings.ocr_recognition_threshold,
    }
    if settings.ocr_batch_size is not None:
        options["batch_size"] = settings.ocr_batch_size
    return options


def _cancellable(reader, token: CancellationToken):
    cancel = getattr(reader, "cancel", None)
    if callable(cancel):
        token.add_cancel_callback(cancel)
    return reader


def _announce_reader(reader, model_id: str, sink: ProgressSink) -> None:
    runtime = dict(reader.runtime_report)
    runtime["model_id"] = model_id
    _announce_runtime(runtime, sink)


def _images_ocr_operation(
    state: AppState,
    *,
    resume: bool,
    images: list[str] | None = None,
    detection_threshold: float | None = None,
) -> Operation:
    """The scan again, this time reading the text off every image."""

    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        _ensure_ocr_runtime(sink, token)
        options = _ocr_options(state, sink, token, detection_threshold)

        def open_reader() -> object:
            second = _cancellable(images_mod.get_ocr(reads_crops=True, **options), token)
            _announce_reader(second, RECOGNIZER_CHECKPOINT.model_id, sink)
            return second

        reader = _cancellable(images_mod.get_ocr(locating=True, **options), token)
        try:
            _announce_reader(reader, DETECTOR_CHECKPOINT.model_id, sink)
            result = extract_images(
                state.project,
                reader,
                sink=sink,
                token=token,
                open_reader=open_reader,
                resume=resume,
                images=images,
            )
            return _scan_summary(result)
        finally:
            close_reader = getattr(reader, "close", None)
            if callable(close_reader):
                close_reader()

    return _reloading(state, run)


def _images_ocr(state: AppState) -> Operation:
    """Carry on from whatever an earlier scan settled."""
    return _images_ocr_operation(state, resume=True)


def _images_ocr_rescan(state: AppState) -> Operation:
    """Read every picture again, whatever is already on file."""
    return _images_ocr_operation(state, resume=False)


def _images_ocr_selected(
    state: AppState,
    *,
    images: list[str],
    detection_threshold: float | None = None,
) -> Operation:
    """Read the pictures named again, and nothing else, at the threshold asked for."""
    listed = (
        set() if state.project is None
        else {str(entry.get("image")) for entry in load_inventory(state.project)}
    )
    if (
        not isinstance(images, list)
        or not images
        or any(not isinstance(image, str) or image not in listed for image in images)
    ):
        raise ValidationFailure("Only pictures the scan listed can be scanned again.")
    if detection_threshold is not None and (
        isinstance(detection_threshold, bool)
        or not isinstance(detection_threshold, int | float)
        or not 0 <= detection_threshold <= 1
    ):
        raise ValidationFailure("The detection threshold must be from 0 to 1.")
    return _images_ocr_operation(
        state,
        resume=False,
        images=images,
        detection_threshold=None if detection_threshold is None else float(detection_threshold),
    )


def _eraser_model(sink: ProgressSink, token: CancellationToken) -> Path:
    def report_download(completed: int, total: int) -> None:
        token.raise_if_cancelled()
        sink.publish(
            ProgressEvent(
                "render-model-download",
                completed,
                total,
                "Downloading the image eraser",
                detail=f"{completed / 1_000_000:.0f} MB / {total / 1_000_000:.0f} MB",
            )
        )

    model = images_mod.eraser_model_path()
    return model if model.is_file() else render_mod.download_model(report_download)


def _render_summary(run) -> dict:
    for result in run.results:
        if result.reason == PAINTED_BY_HAND:
            _log.info(
                "Left %s as it is: it was painted by hand in the image editor. "
                "Delete its draft to render it again.",
                result.image,
            )
    for image, error in run.errors:
        _log.warning("Could not render %s: %s", image, error)
    statuses = Counter(result.status for result in run.results)
    return {
        "rendered": statuses["rendered"],
        "needs_manual": statuses["needs_manual"],
        "skipped": len(run.results) - statuses["rendered"] - statuses["needs_manual"],
        "failed": len(run.errors),
        "out_dir": str(run.out_dir),
    }


def _images_render(state: AppState) -> Operation:
    """Erase the original text and typeset the translation into every picture."""

    def run(sink: ProgressSink, token: CancellationToken) -> dict:
        project = state.project
        font = typeset_font(project)
        if font is None:
            raise InvalidProject(
                "No usable font. Set [images] font in project.toml to a font with "
                "the coverage this language needs."
            )
        _ensure_ocr_runtime(sink, token)
        renderer = images_mod.get_renderer(_eraser_model(sink, token), font)
        token.add_cancel_callback(renderer.cancel)

        def draw(
            source: Path, order: dict, target: str, destination: Path, size: int | None
        ) -> render_mod.RenderResult:
            result = renderer.render(source, order, target, destination, size)
            return render_mod.RenderResult(
                result["image"], result["status"], result["reason"], result.get("size")
            )

        try:
            rendered = render_pictures(project, draw, sink, token, codec=make_image_codec())
        finally:
            renderer.close()
        return _render_summary(rendered)

    return _reloading(state, run)


JOBS: dict[str, Callable[[AppState], Operation]] = {
    "release": _release,
    "build-patch": _build_patch,
    "proxy-map": _proxy_map,
    "extract": _extract,
    "inject": _inject,
    "patch-font": _patch_font,
    "translate": _translate,
    "glossary": _glossary,
    "transform": _transform,
    "recheck": _recheck,
    "fix-brackets": _fix_brackets,
    "fix-values": _fix_values,
    "images-extract": _images_extract,
    "images-ocr": _images_ocr,
    "images-ocr-rescan": _images_ocr_rescan,
    "images-ocr-selected": _images_ocr_selected,
    "images-render": _images_render,
}
