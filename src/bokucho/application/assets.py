"""Image inventory, OCR units, and automatic drafts into the overlay."""

from __future__ import annotations

import contextlib
import html
import json
import re
import statistics
import tempfile
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from bokucho import images as images_mod
from bokucho import render as render_mod
from bokucho.appdirs import locate_font
from bokucho.application.contracts import (
    CancellationToken,
    NeverCancelled,
    NullProgressSink,
    ProgressEvent,
    ProgressSink,
    RasterCodec,
)
from bokucho.application.errors import (
    InvalidProject,
    JobCancelled,
    MissingDependency,
    OcrRuntimeLost,
    ValidationFailure,
)
from bokucho.application.projects import resolve_game_root
from bokucho.application.text import record_transition
from bokucho.model import Status, StringFile, TextUnit
from bokucho.project import Project
from bokucho.sync.atomic import atomic_write_text, file_lock
from bokucho.sync.json_io import load_string_file

_NULL_PROGRESS = NullProgressSink()
_NEVER_CANCELLED = NeverCancelled()
PAINTED_BY_HAND = "edited by hand in the image editor"


@dataclass(frozen=True, slots=True)
class ImageInventoryResult:
    total: int
    failed: int
    added: int
    inventory_path: Path
    entries: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class RenderRunResult:
    results: tuple[render_mod.RenderResult, ...]
    errors: tuple[tuple[str, str], ...]
    out_dir: Path


def load_image_units(project: Project) -> StringFile:
    path = project.strings_dir / images_mod.IMAGES_STRINGS_FILE
    if path.is_file():
        return load_string_file(path)
    return StringFile(source_file=images_mod.IMAGES_SOURCE_NAME)


def save_image_units(project: Project, string_file: StringFile) -> None:
    project.save_string_file(string_file, images_mod.IMAGES_STRINGS_FILE)


@contextmanager
def held_image_units(project: Project) -> Iterator[StringFile]:
    """The picture units as they are now, kept from every other write until the block ends."""
    with file_lock(project.strings_dir / images_mod.IMAGES_STRINGS_FILE):
        yield load_image_units(project)


def _reading(unit: TextUnit) -> tuple[str, object]:
    return unit.source, unit.engine_ref.get("regions") or None


def _save_scanned(
    project: Project,
    scanned: StringFile,
    touched: Mapping[str, tuple[str, object]],
) -> None:
    """Save what a scan read into every picture nobody lettered since the scan saw it."""
    with held_image_units(project) as current:
        on_disk = {unit.id: unit for unit in current.units}
        for unit in scanned.units:
            if unit.id not in touched:
                continue
            kept = on_disk.get(unit.id)
            if kept is None:
                current.units.append(unit.model_copy(deep=True))
                continue
            if _reading(kept) != touched[unit.id]:
                continue
            kept.source = unit.source
            if regions := unit.engine_ref.get("regions"):
                kept.engine_ref["regions"] = regions
            else:
                kept.engine_ref.pop("regions", None)
        save_image_units(project, current)


def set_images_ignored(project: Project, images: Sequence[str], ignored: bool) -> int:
    """Mark the pictures at ``images`` ignored, or hand them back; how many changed."""
    with held_image_units(project) as units:
        found = [(relative, units.by_id(images_mod.unit_id(relative))) for relative in images]
        if missing := [relative for relative, unit in found if unit is None]:
            raise ValidationFailure(f"No extracted image: {', '.join(missing)}")
        changed = 0
        for _, unit in found:
            if (unit.status is Status.IGNORED) == ignored:
                continue
            status = (
                Status.IGNORED if ignored else Status.MT if unit.target else Status.UNTRANSLATED
            )
            record_transition(unit, status)
            unit.status = status
            changed += 1
        if changed:
            save_image_units(project, units)
    return changed


def typeset_font(project: Project) -> str | None:
    """The file pictures are typeset in: the project's face at the weight it names."""
    font = render_mod.resolve_font(locate_font(project.config.images.font), project.root)
    variation = project.config.images.variation
    if font is None or not variation:
        return font
    from fontTools.ttLib import TTFont

    from bokucho.application.proxy_font import pinned

    source = Path(font)
    face = TTFont(source, fontNumber=0)
    if "fvar" not in face:
        return font
    fixed = project.root / ".bokucho" / "typeset" / f"{source.stem}-{variation}.ttf"
    if fixed.is_file() and fixed.stat().st_mtime >= source.stat().st_mtime:
        return fixed.as_posix()
    try:
        face = pinned(face, variation, source)
    except ValueError as error:
        raise InvalidProject(f"[images] variation: {error}") from error
    fixed.parent.mkdir(parents=True, exist_ok=True)
    partial = fixed.with_suffix(".partial")
    face.save(partial)
    partial.replace(fixed)
    return fixed.as_posix()


def renderable_units(units: StringFile) -> list[TextUnit]:
    """The pictures erase and typeset works on: all but those left as they are."""
    return [
        unit
        for unit in units.units
        if unit.engine_ref.get("image") and unit.status is not Status.IGNORED
    ]


def _color_from_qt_hex(value: object) -> list[int] | None:
    if not isinstance(value, str):
        return None
    color = value.removeprefix("#")
    alpha = 255
    if len(color) == 8:
        try:
            alpha = int(color[:2], 16)
        except ValueError:
            return None
        color = color[2:]
    if len(color) != 6:
        return None
    try:
        rgb = [int(color[index : index + 2], 16) for index in (0, 2, 4)]
    except ValueError:
        return None
    return rgb if alpha == 255 else [*rgb, alpha]


def _typesetting_manifest(unit: TextUnit) -> dict[str, object] | None:
    raw = unit.engine_ref.get("typesetting")
    if not isinstance(raw, dict):
        return None
    region = raw.get("region")
    style = raw.get("style")
    if not (
        isinstance(region, list)
        and len(region) == 4
        and all(isinstance(value, int) for value in region)
    ):
        return None
    x0, y0, x1, y1 = region
    if x0 < 0 or y0 < 0 or x0 >= x1 or y0 >= y1:
        return None
    manifest: dict[str, object] = {"region": region}
    if not isinstance(style, dict):
        return manifest
    exported_style = {
        key: style[key]
        for key in ("font_family", "font_size", "alignment", "stroke_width")
        if key in style
    }
    if isinstance(family := exported_style.get("font_family"), str):
        exported_style["font_family"] = locate_font(family)
    fill = _color_from_qt_hex(style.get("fill"))
    stroke = _color_from_qt_hex(style.get("stroke"))
    if fill is not None:
        exported_style["fill"] = fill
    if stroke is not None and int(style.get("stroke_width", 0)) > 0:
        exported_style["stroke"] = stroke
    if style.get("background"):
        exported_style["background"] = style["background"]
    manifest["style"] = exported_style
    return manifest


def _ocr_manifest(unit: TextUnit) -> dict[str, object] | None:
    """The boxes OCR read, as the picture's own regions."""
    regions = unit.engine_ref.get("regions")
    if not isinstance(regions, list):
        return None
    boxes = [
        region["box"]
        for region in regions
        if isinstance(region, dict) and _valid_box(region.get("box"))
    ]
    if not boxes:
        return None
    return {
        "region": [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ],
        "regions": regions,
    }


def image_manifest(unit: TextUnit) -> dict[str, object] | None:
    """What render works this picture from, or None if nothing marks a region."""
    return _typesetting_manifest(unit) or _ocr_manifest(unit)


def _valid_box(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int) for item in value)
        and value[0] < value[2]
        and value[1] < value[3]
    )


INVENTORY_FILENAME = "images-inventory.json"


def inventory_path(project: Project) -> Path:
    return project.root / INVENTORY_FILENAME


def load_inventory(project: Project) -> tuple[dict[str, object], ...]:
    """Previously-scanned image entries; empty when nothing has been scanned."""
    path = inventory_path(project)
    if not path.is_file():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    return tuple(data) if isinstance(data, list) else ()


def image_slug(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/")
    escaped = normalized.replace("%", "%25").replace("_", "%5F")
    return escaped.replace("/", "__")


def _safe_relative_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise InvalidProject(f"Unsafe image path: {raw}")
    return path


READABLE_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp"})


OCR_REGION_SCALE = 1000
OCR_SAVE_EVERY = 100
OCR_BATCH_PIXELS = 4 * 1024 * 768
OCR_BATCH_GROWTH = 4


def _parsed_regions(answer: str) -> list[object] | None:
    """The grounded array in ``answer``, or ``None`` when there is not one."""
    text = answer.strip()
    if not text.startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        closed = text.rfind("}")
        if closed < 0:
            return None
        try:
            parsed = json.loads(text[: closed + 1] + "]")
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, list) else None


def _pixel_box(box: object, width: int, height: int) -> list[int] | None:
    if not isinstance(box, list) or len(box) != 4:
        return None
    if not all(isinstance(value, int | float) for value in box):
        return None
    x0, y0, x1, y1 = (float(value) / OCR_REGION_SCALE for value in box)
    pixels = [
        max(0, min(width, round(x0 * width))),
        max(0, min(height, round(y0 * height))),
        max(0, min(width, round(x1 * width))),
        max(0, min(height, round(y1 * height))),
    ]
    return pixels if pixels[0] < pixels[2] and pixels[1] < pixels[3] else None


_HTML_BLOCK = re.compile(
    r'data-bbox\s*=\s*"\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*"'
    r'(?P<attrs>[^>]*)>(?P<body>.*?)</div>',
    re.DOTALL,
)
_HTML_TAG = re.compile(r"<[^>]+>")


def _html_regions(answer: str) -> list[dict[str, object]] | None:
    """The blocks in an HTML answer, in the shape the JSON one arrives in."""
    if "data-bbox" not in answer:
        return None
    entries: list[dict[str, object]] = []
    for match in _HTML_BLOCK.finditer(answer):
        if 'data-label="Image"' in match.group("attrs"):
            continue
        body = match.group("body").replace("<br>", "\n").replace("<br/>", "\n")
        text = html.unescape(_HTML_TAG.sub("", body)).strip()
        entries.append(
            {
                "bbox_2d": [int(match.group(index)) for index in (1, 2, 3, 4)],
                "text": text,
            }
        )
    return entries


def ocr_regions(
    answer: str,
    width: object,
    height: object,
) -> tuple[str, list[dict[str, object]]]:
    """Split an OCR answer into its text and the boxes it was read from."""
    parsed = _html_regions(answer)
    if parsed is None:
        parsed = _parsed_regions(answer)
    if parsed is None or not isinstance(width, int) or not isinstance(height, int):
        return answer, []
    regions: list[dict[str, object]] = []
    seen: set[tuple[int, ...]] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        box = _pixel_box(entry.get("bbox_2d"), width, height)
        if not isinstance(text, str) or not text.strip() or box is None:
            continue
        if tuple(box) in seen:
            continue
        seen.add(tuple(box))
        score = entry.get("score")
        if isinstance(score, int | float) and not isinstance(score, bool):
            region = {"box": box, "text": text, "score": float(score)}
        else:
            region = {"box": box, "text": text}
        regions.append(region)
    return "\n".join(str(region["text"]) for region in regions), regions


@dataclass(frozen=True, slots=True)
class _OcrCandidate:
    index: int
    relative: str
    readable_path: Path
    metadata: dict[str, object]
    unit: TextUnit


def _pixels(candidate: _OcrCandidate, floor: int = 0) -> int:
    """What ``candidate`` costs a batch."""
    width = candidate.metadata.get("width")
    height = candidate.metadata.get("height")
    if isinstance(width, int) and isinstance(height, int):
        return max(width * height, floor)
    return floor


def _read_ocr_candidates(
    ocr_reader: Callable[[str], object],
    candidates: Sequence[_OcrCandidate],
) -> tuple[list[str | Exception], bool]:
    """The readings, and whether the batch was taken rather than fallen back on."""
    read_many = getattr(ocr_reader, "read_many", None)
    if callable(read_many):
        try:
            values = list(
                read_many([str(candidate.readable_path) for candidate in candidates])
            )
            if len(values) != len(candidates):
                raise RuntimeError("OCR reader returned the wrong number of results")
            return [str(value).strip() for value in values], True
        except (JobCancelled, OcrRuntimeLost):
            raise
        except Exception:  # noqa: BLE001 - retry separately to isolate the bad asset
            pass

    results: list[str | Exception] = []
    for candidate in candidates:
        try:
            results.append(str(ocr_reader(str(candidate.readable_path))).strip())
        except (JobCancelled, OcrRuntimeLost):
            raise
        except Exception as exc:  # noqa: BLE001 - failures belong to one image
            results.append(exc)
    return results, False


def _close(reader: object) -> None:
    """Let a reader go, if letting go is something it does."""
    close = getattr(reader, "close", None)
    if callable(close):
        close()


def _detect_ocr_candidates(
    detector: object,
    candidates: Sequence[_OcrCandidate],
) -> list[list[list[int]]] | list[Exception]:
    """Boxes for every candidate, falling back to one at a time on a refusal."""
    paths = [str(candidate.readable_path) for candidate in candidates]
    try:
        located = list(detector.detect_many(paths))
        if len(located) != len(candidates):
            raise RuntimeError("OCR detector returned the wrong number of results")
        return located
    except (JobCancelled, OcrRuntimeLost):
        raise
    except Exception:  # noqa: BLE001 - retry separately to isolate the bad asset
        pass
    results: list[list[list[int]]] | list[Exception] = []
    for path in paths:
        try:
            [boxes] = detector.detect_many([path])
            results.append(boxes)
        except (JobCancelled, OcrRuntimeLost):
            raise
        except Exception as exc:  # noqa: BLE001 - failures belong to one image
            results.append(exc)
    return results


def _read_located_batch(
    reader: object,
    paths: Sequence[str],
    boxes: Sequence[list[list[int]]],
) -> list[str | Exception]:
    """The words in every located picture, falling back to one at a time."""

    def ask(
        asked: Sequence[str], located: Sequence[list[list[int]]]
    ) -> list[str]:
        answers = list(reader.read_located(list(asked), list(located)))
        if len(answers) != len(asked):
            raise RuntimeError("OCR reader returned the wrong number of results")
        return [str(answer) for answer in answers]

    try:
        return list(ask(paths, boxes))
    except (JobCancelled, OcrRuntimeLost):
        raise
    except Exception:  # noqa: BLE001 - retry separately to isolate the bad asset
        pass
    results: list[str | Exception] = []
    for path, located in zip(paths, boxes, strict=True):
        try:
            results.extend(ask([path], [located]))
        except (JobCancelled, OcrRuntimeLost):
            raise
        except Exception as exc:  # noqa: BLE001 - failures belong to one image
            results.append(exc)
    return results


def _scan_is_settled(entry: dict[str, object], unit: TextUnit | None) -> bool:
    """Whether an earlier scan finished with this picture."""
    if entry.get("error"):
        return True
    if unit is not None and (unit.engine_ref.get("regions") or unit.source.strip()):
        return True
    boxes = entry.get("boxes")
    return isinstance(boxes, list) and not boxes


_LocatedWork = tuple[_OcrCandidate, list[list[int]], dict[str, object]]


class _ImageScan:
    """One pass over a game's pictures: what it read, and what it has written down."""

    def __init__(
        self,
        project: Project,
        ocr_reader: Callable[[str], object] | None,
        sink: ProgressSink,
        token: CancellationToken,
        open_reader: Callable[[], object] | None,
        *,
        resume: bool,
        images: Collection[str] | None,
    ) -> None:
        self.project = project
        self.ocr_reader = ocr_reader
        self.sink = sink
        self.token = token
        self.open_reader = open_reader
        self.root = resolve_game_root(project)
        paths = images_mod.iter_images(self.root)
        self.order = {
            path.relative_to(self.root).as_posix(): index for index, path in enumerate(paths)
        }
        self.earlier = {
            str(entry.get("image")): dict(entry)
            for entry in load_inventory(project)
            if str(entry.get("image")) in self.order
        }
        self.units = load_image_units(project)
        self.paths = self._chosen(paths, images)
        self.settled = self._settled() if images is None and resume else set()
        self.entries: list[dict[str, object]] = []
        self.added = self.failed = 0
        self.units_changed = False
        self.entries_changed = False
        self.saved_entries = 0
        self.touched: dict[str, tuple[str, object]] = {}
        self.pending: list[_OcrCandidate] = []
        self.pending_pixels = 0
        self.two_phase = open_reader is not None and ocr_reader is not None
        self.located_work: list[_LocatedWork] = []
        read_many = getattr(ocr_reader, "read_many", None)
        self.batch_size = (
            max(1, int(getattr(ocr_reader, "batch_size", 1))) if callable(read_many) else 1
        )
        self.batch_ceiling = self.batch_size * OCR_BATCH_GROWTH
        self.largest_taken = 0
        self.pixel_floor = int(getattr(ocr_reader, "visual_floor_pixels", 0) or 0)

    def _chosen(self, paths: list[Path], images: Collection[str] | None) -> list[Path]:
        if images is None:
            return paths
        ignored = {
            str(unit.engine_ref.get("image"))
            for unit in self.units.units
            if unit.status is Status.IGNORED
        }
        wanted = set(images) - ignored
        return [path for path in paths if path.relative_to(self.root).as_posix() in wanted]

    def _settled(self) -> set[str]:
        return {
            relative
            for relative, previous in self.earlier.items()
            if _scan_is_settled(previous, self.units.by_id(images_mod.unit_id(relative)))
        }

    def inventory(self) -> list[dict[str, object]]:
        """This scan's entries, and the earlier ones for every picture it has not reached."""
        walked = {str(entry.get("image")) for entry in self.entries}
        kept = [entry for relative, entry in self.earlier.items() if relative not in walked]
        return sorted(
            [*self.entries, *kept],
            key=lambda entry: self.order.get(str(entry.get("image")), len(self.order)),
        )

    def publish(self, index: int, relative: str) -> None:
        self.sink.publish(
            ProgressEvent(
                "images.scan",
                index,
                len(self.paths),
                "images.scan.item.completed",
                detail=relative,
            )
        )

    def _publish_reading(self, completed: int, total: int, relatives: list[str]) -> None:
        detail = relatives[0] if len(relatives) == 1 else f"{relatives[0]} +{len(relatives) - 1}"
        self.sink.publish(
            ProgressEvent(
                "images.scan.reading",
                completed,
                total,
                "images.scan.batch.reading",
                detail=detail,
            )
        )

    def persist(self) -> None:
        """Write down what has been read so far."""
        if len(self.entries) != self.saved_entries or self.entries_changed:
            atomic_write_text(
                inventory_path(self.project),
                json.dumps(self.inventory(), ensure_ascii=False, indent=1),
            )
            self.saved_entries = len(self.entries)
            self.entries_changed = False
        if self.units_changed:
            _save_scanned(self.project, self.units, self.touched)
            self.touched.clear()
            self.units_changed = False

    def apply_reading(self, candidate: _OcrCandidate, result: str) -> None:
        """Put one answer where it belongs: the unit's source and regions."""
        text, regions = ocr_regions(
            result,
            candidate.metadata.get("width"),
            candidate.metadata.get("height"),
        )
        unit = candidate.unit
        seen = _reading(unit)
        if unit.source != text:
            if text and not unit.source.strip():
                self.added += 1
            unit.source = text
            self.units_changed = True
            self.touched.setdefault(unit.id, seen)
        if regions != (unit.engine_ref.get("regions") or []):
            if regions:
                unit.engine_ref["regions"] = regions
            else:
                unit.engine_ref.pop("regions", None)
            self.units_changed = True
            self.touched.setdefault(unit.id, seen)

    def flush_pending(self) -> None:
        if not self.pending or self.ocr_reader is None:
            return
        self.token.raise_if_cancelled()
        self._publish_reading(
            self.pending[0].index - 1,
            len(self.paths),
            [candidate.relative for candidate in self.pending],
        )
        if self.two_phase:
            self._locate_pending()
        else:
            self._read_pending()
        self.pending.clear()
        self.pending_pixels = 0

    def _locate_pending(self) -> None:
        located = _detect_ocr_candidates(self.ocr_reader, self.pending)
        for candidate, found in zip(self.pending, located, strict=True):
            entry: dict[str, object] = {"image": candidate.relative, **candidate.metadata}
            if isinstance(found, Exception):
                entry["error"] = str(found)
                self.failed += 1
            else:
                entry["boxes"] = found
                if found:
                    self.located_work.append((candidate, found, entry))
            self.entries.append(entry)
            self.publish(candidate.index, candidate.relative)

    def _read_pending(self) -> None:
        filled = len(self.pending) >= self.batch_size
        results, batched = _read_ocr_candidates(self.ocr_reader, self.pending)
        if not batched:
            self.batch_ceiling = self.batch_size = max(
                1, self.largest_taken or self.batch_size // 2
            )
        else:
            self.largest_taken = max(self.largest_taken, len(self.pending))
            if filled and self.batch_size < self.batch_ceiling:
                self.batch_size = min(self.batch_ceiling, self.batch_size * 2)
        for candidate, result in zip(self.pending, results, strict=True):
            entry: dict[str, object] = {"image": candidate.relative, **candidate.metadata}
            if isinstance(result, Exception):
                entry["error"] = str(result)
                self.failed += 1
                self.entries.append(entry)
            else:
                self.entries.append(entry)
                self.apply_reading(candidate, result)
            self.publish(candidate.index, candidate.relative)

    def _read_located(self) -> None:
        """Read what was located, in batches drawn from as many pictures as fit."""
        if not self.located_work:
            return
        reader = self.open_reader()
        try:
            limit = max(1, int(getattr(reader, "batch_size", 1) or 1))
            total = sum(len(boxes) for _, boxes, _ in self.located_work)
            batch: list[_LocatedWork] = []
            read = 0
            for item in self.located_work:
                batch.append(item)
                if sum(len(boxes) for _, boxes, _ in batch) >= limit:
                    read = self._read_located_group(reader, batch, read, total)
                    batch = []
            self._read_located_group(reader, batch, read, total)
        finally:
            _close(reader)

    def _read_located_group(
        self, reader: object, batch: list[_LocatedWork], read: int, total: int
    ) -> int:
        if not batch:
            return read
        self.token.raise_if_cancelled()
        self._publish_reading(read, total, [candidate.relative for candidate, _, _ in batch])
        answers = _read_located_batch(
            reader,
            [str(candidate.readable_path) for candidate, _, _ in batch],
            [boxes for _, boxes, _ in batch],
        )
        for (candidate, boxes, entry), answer in zip(batch, answers, strict=True):
            if isinstance(answer, Exception):
                entry["error"] = str(answer)
                self.entries_changed = True
                self.failed += 1
            else:
                self.apply_reading(candidate, answer)
            read += len(boxes)
        self.persist()
        return read

    def _pass_over(self, index: int, entry: dict[str, object], *, failed: bool = False) -> None:
        """Note a picture that is not read, after reading what waits before it."""
        self.flush_pending()
        self.entries.append(entry)
        if failed:
            self.failed += 1
        self.publish(index, str(entry["image"]))

    def _unit_for(self, relative: str, metadata: dict[str, object]) -> TextUnit:
        unit = self.units.by_id(images_mod.unit_id(relative))
        if unit is not None:
            return unit
        unit = TextUnit(
            id=images_mod.unit_id(relative),
            source="",
            kind="image",
            engine_ref={"image": relative, "format": metadata["format"]},
        )
        self.units.units.append(unit)
        self.units_changed = True
        self.touched.setdefault(unit.id, ("", None))
        return unit

    @staticmethod
    def _readable(path: Path, index: int, scratch_dir: Path) -> Path:
        readable = path
        if path.suffix.lower() not in READABLE_IMAGE_SUFFIXES:
            readable = scratch_dir / f"{index}.png"
            images_mod.to_png(path, readable)
        return images_mod.opaque_view(readable, scratch_dir / f"{index}.png")

    def _queue(self, candidate: _OcrCandidate) -> None:
        cost = _pixels(candidate, self.pixel_floor)
        if self.pending and self.pending_pixels + cost > OCR_BATCH_PIXELS:
            self.flush_pending()
        self.pending.append(candidate)
        self.pending_pixels += cost
        if len(self.pending) >= self.batch_size:
            self.flush_pending()
        if len(self.entries) - self.saved_entries >= OCR_SAVE_EVERY:
            self.persist()

    def _visit(self, index: int, path: Path, scratch_dir: Path) -> None:
        self.token.raise_if_cancelled()
        relative = path.relative_to(self.root).as_posix()
        if relative in self.settled:
            self.publish(index, relative)
            return
        try:
            metadata = images_mod.inspect(path)
        except Exception as exc:  # noqa: BLE001 - isolate per-asset failures
            self._pass_over(index, {"image": relative, "error": str(exc)}, failed=True)
            return
        unit = self._unit_for(relative, metadata)
        if self.ocr_reader is None or unit.status is Status.IGNORED:
            self._pass_over(index, {"image": relative, **metadata})
            return
        try:
            readable = self._readable(path, index, scratch_dir)
        except Exception as exc:  # noqa: BLE001 - isolate conversion failures
            entry = {"image": relative, **metadata, "error": str(exc)}
            self._pass_over(index, entry, failed=True)
            return
        self._queue(_OcrCandidate(index, relative, readable, metadata, unit))

    def _drop_unlocated(self, scratch_dir: Path) -> None:
        keep = {candidate.readable_path for candidate, _, _ in self.located_work}
        for leftover in scratch_dir.iterdir():
            if leftover.is_file() and leftover not in keep:
                leftover.unlink(missing_ok=True)

    def run(self) -> ImageInventoryResult:
        with tempfile.TemporaryDirectory(prefix="bokucho-ocr-") as scratch:
            scratch_dir = Path(scratch)
            for index, path in enumerate(self.paths, start=1):
                self._visit(index, path, scratch_dir)
            self.flush_pending()
            if self.two_phase:
                self.persist()
                _close(self.ocr_reader)
                self._drop_unlocated(scratch_dir)
                self._read_located()
        self.persist()
        listed = self.inventory()
        return ImageInventoryResult(
            len(listed),
            self.failed,
            self.added,
            inventory_path(self.project),
            tuple(listed),
        )


def extract_images(
    project: Project,
    ocr_reader: Callable[[str], object] | None = None,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    open_reader: Callable[[], object] | None = None,
    resume: bool = False,
    images: Collection[str] | None = None,
) -> ImageInventoryResult:
    """Inventory game images and optionally add OCR-backed image text units."""
    scan: _ImageScan | None = None
    try:
        scan = _ImageScan(
            project, ocr_reader, sink, token, open_reader, resume=resume, images=images
        )
        return scan.run()
    finally:
        if scan is not None:
            with contextlib.suppress(OSError):
                scan.persist()
        _close(ocr_reader)


def render_images(
    project: Project,
    font_path: str,
    inpaint_fn,
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    *,
    codec: RasterCodec,
) -> RenderRunResult:
    """Erase + typeset every image unit that has a region and a translation."""

    def draw(
        source: Path, order: dict, target: str, destination: Path, size: int | None
    ) -> render_mod.RenderResult:
        return render_mod.render_image(
            source, order, target, font_path, inpaint_fn, destination, size
        )

    return render_pictures(project, draw, sink, token, codec=codec)


def render_pictures(
    project: Project,
    draw: Callable[[Path, dict, str, Path, int | None], render_mod.RenderResult],
    sink: ProgressSink = _NULL_PROGRESS,
    token: CancellationToken = _NEVER_CANCELLED,
    *,
    codec: RasterCodec,
) -> RenderRunResult:
    """Have ``draw`` letter each picture, one size per set; a bad picture ends only itself."""
    lettering = _Lettering(project, draw, codec)
    todo = renderable_units(load_image_units(project))
    for index, unit in enumerate(todo, start=1):
        token.raise_if_cancelled()
        relative = str(unit.engine_ref["image"])
        lettering.letter(unit, relative)
        sink.publish(
            ProgressEvent(
                "images.render",
                index,
                len(todo),
                "images.render.item.completed",
                detail=relative,
            )
        )
    lettering.match_sizes(token)
    return RenderRunResult(tuple(lettering.results), tuple(lettering.errors), lettering.out_dir)


class _Lettering:
    """One render run: each picture's result, and the pictures of a set to letter alike."""

    def __init__(
        self,
        project: Project,
        draw: Callable[[Path, dict, str, Path, int | None], render_mod.RenderResult],
        codec: RasterCodec,
    ) -> None:
        self.project = project
        self.draw = draw
        self.codec = codec
        self.root = resolve_game_root(project)
        self.out_dir = project.root / "out"
        self.results: list[render_mod.RenderResult] = []
        self.errors: list[tuple[str, str]] = []
        self.sets: dict[tuple[Path, int, int], list[tuple[int, Path, dict, str]]] = {}

    def letter(self, unit: TextUnit, relative: str) -> None:
        native = Path(*PurePosixPath(relative.replace("\\", "/")).parts)
        try:
            self._letter(unit, relative, native)
        except (JobCancelled, MissingDependency):
            raise
        except Exception as exc:  # noqa: BLE001 - one bad picture must not end the run
            self.errors.append((relative, f"{type(exc).__name__}: {exc}"))

    def _letter(self, unit: TextUnit, relative: str, native: Path) -> None:
        from bokucho.application.image_editor_saving import painted_by_hand

        manifest = image_manifest(unit)
        if manifest is None:
            self.results.append(render_mod.RenderResult(relative, "skipped", "no region"))
            return
        if images_mod.frame_count(self.root / native) > 1:
            self.results.append(render_mod.RenderResult(relative, "skipped", "animation"))
            return
        if painted_by_hand(self.project, relative, self.codec):
            self.results.append(render_mod.RenderResult(relative, "skipped", PAINTED_BY_HAND))
            return
        order = {**manifest, "image": relative, "source": unit.source}
        result = self.draw(self.root / native, order, unit.target, self.out_dir / native, None)
        self.results.append(result)
        if result.size is not None:
            shape = images_mod.inspect(self.root / native)
            key = (native.parent, shape["width"], shape["height"])
            self.sets.setdefault(key, []).append(
                (len(self.results) - 1, native, order, unit.target)
            )

    def match_sizes(self, token: CancellationToken) -> None:
        """Letter again each picture of a set drawn at another size than most of the set."""
        # ponytail: a picture that changes size is erased and drawn twice; measure
        # ahead of drawing if inpainting a set twice gets slow.
        results = self.results
        for members in self.sets.values():
            if len(members) < 2:
                continue
            shared = statistics.median_low(results[at].size for at, *_ in members)
            for at, native, order, target in members:
                if results[at].size == shared:
                    continue
                token.raise_if_cancelled()
                try:
                    results[at] = self.draw(
                        self.root / native, order, target, self.out_dir / native, shared
                    )
                except (JobCancelled, MissingDependency):
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad picture must not end the run
                    self.errors.append((results[at].image, f"{type(exc).__name__}: {exc}"))
