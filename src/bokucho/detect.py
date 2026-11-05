"""Engine detection — filename and directory sniffing, zero engine dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIDENT = 0.8


@dataclass(frozen=True)
class Signature:
    """How one engine looks on disk."""

    engine: str
    label: str
    supported: bool
    hint: str
    patterns: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class Detection:
    """One engine's verdict for a scanned directory."""

    engine: str
    label: str
    confidence: float
    supported: bool
    evidence: list[str]
    hint: str


SIGNATURES: tuple[Signature, ...] = (
    Signature(
        engine="livemaker",
        label="LiveMaker / LiveNovel",
        supported=True,
        hint='Supported — set [engine] type = "livemaker" and point game_path here.',
        patterns=(("**/live.lpb", 0.95), ("**/*.lsb", 0.95), ("**/*.lpb", 0.7)),
    ),
    Signature(
        engine="renpy",
        label="Ren'Py",
        supported=True,
        hint='Supported — set [engine] type = "renpy" and sdk_path to the Ren\'Py SDK.',
        patterns=(
            ("game/*.rpy", 0.95),
            ("renpy/__init__.py", 0.9),
            ("game/*.rpyc", 0.9),
            ("game/*.rpa", 0.85),
        ),
    ),
    Signature(
        engine="kirikiri",
        label="KiriKiri / KAG",
        supported=False,
        hint="Not supported yet — GARbro can unpack .xp3 archives for manual work.",
        patterns=(("**/data.xp3", 0.95), ("**/*.xp3", 0.9)),
    ),
    Signature(
        engine="rpgmaker-mv",
        label="RPG Maker MV",
        supported=True,
        hint=(
            'Supported — set [engine] type = "rpgmaker-mv" and point game_path at '
            "the game folder."
        ),
        patterns=(
            ("**/*.rpgproject", 0.95),
            ("www/data/System.json", 0.9),
        ),
    ),
    Signature(
        engine="rpgmaker-mz",
        label="RPG Maker MZ",
        supported=True,
        hint=(
            'Supported — set [engine] type = "rpgmaker-mz" and point game_path at '
            "the game folder."
        ),
        patterns=(
            ("**/*.rmmzproject", 0.95),
            ("data/System.json", 0.9),
        ),
    ),
    Signature(
        engine="rpgmaker-vx-ace",
        label="RPG Maker VX Ace",
        supported=True,
        hint=(
            'Supported — set [engine] type = "rpgmaker-vx-ace" and point game_path at '
            "the game folder or packed archive. Install support with: "
            'pip install "bokucho[rpgmaker]"'
        ),
        patterns=(
            ("Data/System.rvdata2", 0.9),
            ("**/*.rgss3a", 0.9),
        ),
    ),
    Signature(
        engine="rpgmaker-xp",
        label="RPG Maker XP",
        supported=True,
        hint=(
            'Supported — set [engine] type = "rpgmaker-xp" and point game_path at '
            "the game folder or packed archive. Install support with: "
            'pip install "bokucho[rpgmaker]"'
        ),
        patterns=(
            ("Data/System.rxdata", 0.9),
            ("**/*.rgssad", 0.9),
        ),
    ),
    Signature(
        engine="rpgmaker-vx",
        label="RPG Maker VX",
        supported=True,
        hint=(
            'Supported — set [engine] type = "rpgmaker-vx" and point game_path at '
            "the game folder or packed archive. Install support with: "
            'pip install "bokucho[rpgmaker]"'
        ),
        patterns=(
            ("Data/System.rvdata", 0.9),
            ("**/*.rgss2a", 0.9),
        ),
    ),
    Signature(
        engine="unity",
        label="Unity",
        supported=False,
        hint="Not supported yet — AssetStudio or UABE can extract text assets.",
        patterns=(("**/UnityPlayer.dll", 0.9), ("**/*_Data/globalgamemanagers", 0.9)),
    ),
    Signature(
        engine="godot",
        label="Godot",
        supported=False,
        hint="Not supported yet — godotpcktool can unpack .pck archives.",
        patterns=(("**/project.godot", 0.95), ("**/*.pck", 0.7)),
    ),
)


_PACKED_SUFFIXES = frozenset({".exe", ".dat"})

_PACKED_CONFIDENCE = 0.95


def _is_livemaker_archive(path: Path) -> bool:
    """Whether ``path``'s bytes are a LiveMaker archive."""
    try:
        with path.open("rb") as handle:
            head = handle.read(2)
            if head == b"vf":
                return True
            if head != b"MZ":
                return False
            handle.seek(-2, os.SEEK_END)
            return handle.read(2) == b"lv"
    except OSError:
        return False


def _packed_livemaker(target: Path) -> list[Path]:
    """Packed LiveMaker archives at ``target`` — itself, or top-level inside it."""
    # ponytail: livemaker is the only engine with a byte-level probe today, so
    # this is one step rather than a Signature field. Add the field when a
    # second engine needs one.
    if target.is_file():
        return [target] if _is_livemaker_archive(target) else []
    try:
        candidates = sorted(target.iterdir())
    except OSError:
        return []
    return [
        item
        for item in candidates
        if item.is_file()
        and item.suffix.lower() in _PACKED_SUFFIXES
        and _is_livemaker_archive(item)
    ]


def detect(path: Path) -> list[Detection]:
    """Engines that could explain ``path``, most likely first."""
    target = Path(path)
    root = target if target.is_dir() else target.parent
    packed = _packed_livemaker(target)
    found: list[Detection] = []
    for sig in SIGNATURES:
        confidence = 0.0
        evidence: list[str] = []
        if sig.engine == "livemaker" and packed:
            confidence = _PACKED_CONFIDENCE
            evidence = [item.name for item in packed]
        for pattern, weight in sig.patterns:
            # ponytail: next() short-circuits a *match*, but a miss still walks
            # the whole subtree, and most patterns miss — so a big game tree is
            # scanned once per non-matching pattern. Fine for a one-shot CLI
            # call; if it ever drags, collect the tree once (rglob) and match
            # patterns against that list instead of re-globbing per pattern.
            hit = next(root.glob(pattern), None)
            if hit is None:
                continue
            rel = hit.relative_to(root).as_posix()
            if rel not in evidence:
                evidence.append(rel)
            confidence = max(confidence, weight)
        if evidence:
            found.append(
                Detection(
                    engine=sig.engine,
                    label=sig.label,
                    confidence=confidence,
                    supported=sig.supported,
                    evidence=evidence,
                    hint=sig.hint,
                )
            )
    return sorted(found, key=lambda d: (d.confidence, d.supported), reverse=True)
