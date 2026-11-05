"""Score a scan against ``tools/ocr-ground-truth.json``."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bokucho.application.assets import (  # noqa: E402
    load_image_units,
    resolve_game_root,
)
from bokucho.application.errors import MultiFrameImage  # noqa: E402
from bokucho.images import iter_images, unit_id  # noqa: E402
from bokucho.project import Project  # noqa: E402

GROUND_TRUTH = Path(__file__).with_name("ocr-ground-truth.json")
SAMPLE_FILENAME = "ocr-sample.json"
SAMPLE_STRIDE = 27
MISALIGNED_ERROR_RATE = 0.5


def _is_cjk(character: str) -> bool:
    """Whether ``character`` is the sort of text that gets translated."""
    code = ord(character)
    return (
        0x3040 <= code <= 0x30FF
        or 0x31F0 <= code <= 0x31FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def cjk(text: str) -> str:
    """``text`` folded to the characters a translation would have to carry."""
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", text)
        if _is_cjk(character)
    )


def edits(read: str, expected: str) -> int:
    """Levenshtein distance, one row at a time because the strings are short."""
    if read == expected:
        return 0
    previous = list(range(len(expected) + 1))
    for index, character in enumerate(read, start=1):
        current = [index]
        for position, wanted in enumerate(expected, start=1):
            current.append(
                min(
                    previous[position] + 1,
                    current[position - 1] + 1,
                    previous[position - 1] + (character != wanted),
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True, slots=True)
class Score:
    """What forty-eight pieces of lettering say about one configuration."""

    lettered: int
    found: int
    blank: int
    invented: int
    errors: int
    reference: int
    unscored: tuple[str, ...]
    unidentified: tuple[str, ...] = ()

    @property
    def character_error_rate(self) -> float:
        return self.errors / self.reference if self.reference else 0.0

    def report(self) -> str:
        rate = f"{self.character_error_rate:.1%}"
        lines = [
            f"pictures with text found     {self.found} of {self.lettered}",
            f"invented on a blank picture  {self.invented} of {self.blank}",
            f"character error rate         {rate}  "
            f"({self.errors} edit(s) over {self.reference} character(s))",
        ]
        if self.unidentified:
            lines.append(_listed("not identified", self.unidentified))
        if self.unscored:
            lines.append(_listed("not scanned", self.unscored))
        lines.append(self._caveat())
        return "\n".join(line for line in lines if line)

    def _caveat(self) -> str:
        """What the number above is not saying, when it is not saying much."""
        if not self.lettered:
            return (
                f"\nNothing was scored. Fill in the nulls in "
                f"{SAMPLE_FILENAME}, or scan a game this ground truth was "
                "written for."
            )
        if not self.found:
            return (
                "\nNo text was read on any of them, so this is not a reading "
                "score: the project holds boxes without words, or the scan was "
                "run with OCR off."
            )
        if self.character_error_rate > MISALIGNED_ERROR_RATE:
            return (
                "\nA rate this high is what a wrong mapping looks like, not a "
                "bad reader: every configuration in the findings scored "
                f"between two and five percent. Check {SAMPLE_FILENAME}."
            )
        return ""


def _listed(label: str, entries: tuple[str, ...]) -> str:
    shown = ", ".join(entries[:4]) + (" ..." if len(entries) > 4 else "")
    return f"{label:<28} {len(entries)}: {shown}"


def sampled_walk(project: Project, stride: int) -> dict[int, str]:
    """The pictures the findings sampled: every ``stride``-th of the sorted walk."""
    root = resolve_game_root(project)
    walk = iter_images(root)
    return {
        index // stride: path.relative_to(root).as_posix()
        for index, path in enumerate(walk)
        if index % stride == 0
    }


def resolve_sample(
    truth: dict[str, list[str]], walk: dict[int, str]
) -> dict[str, str | None]:
    """Which picture each ground-truth index is, matched on what is written on it."""
    resolved: dict[str, str | None] = {}
    folded = {position: _fold(name) for position, name in walk.items()}
    for index in sorted(truth, key=int):
        resolved[index] = None
        if not truth[index]:
            continue
        key = _fold(max(truth[index], key=len)).strip()
        if len(key) < 2:
            continue
        hits = [
            position for position, name in folded.items() if key in name
        ]
        if len(hits) == 1:
            resolved[index] = walk[hits[0]]
    return resolved


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def load_sample(
    project: Project, truth: dict[str, list[str]], stride: int
) -> dict[str, str | None]:
    """The mapping beside the project, resolved the first time and kept after."""
    path = project.root / SAMPLE_FILENAME
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            return {index: saved.get(index) for index in truth}
    resolved = resolve_sample(truth, sampled_walk(project, stride))
    path.write_text(
        json.dumps(resolved, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return resolved


def score(
    project: Project, truth: dict[str, list[str]], sample: dict[str, str | None]
) -> Score:
    """Compare what the scan wrote down against what was read by eye."""
    units = {unit.id: unit for unit in load_image_units(project).units}
    lettered = found = blank = invented = errors = reference = 0
    unidentified: list[str] = []
    unscored: list[str] = []
    for index, pieces in sorted(truth.items(), key=lambda item: int(item[0])):
        relative = sample.get(index)
        if relative is None:
            unidentified.append(f"{index} ({' | '.join(pieces) or 'blank'})")
            continue
        unit = units.get(unit_id(relative))
        if unit is None:
            unscored.append(relative)
            continue
        read = cjk(unit.source)
        expected = cjk("".join(pieces))
        if not expected:
            blank += 1
            invented += bool(read)
            continue
        lettered += 1
        found += bool(read)
        errors += edits(read, expected)
        reference += len(expected)
    return Score(
        lettered,
        found,
        blank,
        invented,
        errors,
        reference,
        tuple(unscored),
        tuple(unidentified),
    )


def draw(
    project: Project,
    sample: dict[str, str | None],
    truth: dict,
    destination: Path,
) -> int:
    """Each scored picture with its boxes on it, for the verdict only eyes give."""
    from PIL import Image, ImageDraw

    from bokucho import images as images_mod

    root = resolve_game_root(project)
    units = {unit.id: unit for unit in load_image_units(project).units}
    destination.mkdir(parents=True, exist_ok=True)
    drawn = 0
    for index in sorted(truth, key=int):
        relative = sample.get(index)
        unit = units.get(unit_id(relative)) if relative is not None else None
        if unit is None:
            continue
        source = root / Path(relative)
        readable = destination / f"{index}.png"
        try:
            images_mod.to_png(source, readable)
        except MultiFrameImage:
            continue
        with Image.open(readable) as picture:
            canvas = picture.convert("RGB")
        pen = ImageDraw.Draw(canvas)
        for region in unit.engine_ref.get("regions") or []:
            box = region.get("box")
            if isinstance(box, list) and len(box) == 4:
                pen.rectangle(box, outline=(255, 0, 0))
        canvas.save(readable)
        drawn += 1
    return drawn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="A project that has been scanned.")
    parser.add_argument(
        "--stride",
        type=int,
        default=SAMPLE_STRIDE,
        help="How far apart the sampled pictures are in the sorted walk.",
    )
    parser.add_argument(
        "--draw",
        type=Path,
        help="Write each scored picture out with its boxes drawn on it.",
    )
    args = parser.parse_args(argv)

    if not GROUND_TRUTH.is_file():
        print(
            f"{GROUND_TRUTH} is kept out of the repository: copy it there to score a scan.",
            file=sys.stderr,
        )
        return 2
    project = Project.load(args.project)
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["pictures"]
    sample = load_sample(project, truth, args.stride)
    print(score(project, truth, sample).report())
    if args.draw is not None:
        print(f"boxes drawn on {draw(project, sample, truth, args.draw)} picture(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
