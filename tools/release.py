"""Write a tagged release's notes and checksums for the GitHub release beside the ZIP."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = ROOT / "dist"
PORTABLE_ZIP = DIST_ROOT / "Bokucho-Windows-x64.zip"
NOTES = DIST_ROOT / "release-notes.md"
RELEASE_TAGS = "v*"
CONVENTIONAL_SUBJECT = re.compile(
    r"(?P<type>[a-z]+)(?:\((?P<scope>[^)]+)\))?!?: (?P<description>.+)"
)
# The notes list what a release adds (+) and changes or fixes (*); refactors,
# documentation, tests and tooling are not part of what users get.
MARKERS = {"feat": "+", "fix": "*", "perf": "*"}
CORE = "Core"
SECTIONS = (CORE, "Engines", "Images", "Release", "GUI")
SECTION_OF_SCOPE = {
    "cs2": "Engines",
    "engines": "Engines",
    "kirikiri": "Engines",
    "livemaker": "Engines",
    "renpy": "Engines",
    "rpgmaker": "Engines",
    "rpgmv": "Engines",
    "unity": "Engines",
    "images": "Images",
    "ocr": "Images",
    "render": "Images",
    "release": "Release",
    "patcher": "Release",
    "gui": "GUI",
    "web": "GUI",
    "webapp": "GUI",
    "windows": "GUI",
    "i18n": "GUI",
}
CHECKSUM_FILES = {"SHA2-256SUMS": "sha256", "SHA2-512SUMS": "sha512"}


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def commit_subjects(tag: str, repository: Path = ROOT) -> list[str]:
    """Subjects of the commits a tag adds since the release tag before it, newest first.

    A commit whose body lists conventional subjects as ``- `` lines, as a move to
    the public repository squashed into one commit does, gives those instead of
    its own subject.
    """
    previous = _git(
        repository, "describe", "--tags", "--abbrev=0", f"--match={RELEASE_TAGS}", f"{tag}^"
    )
    revisions = f"{previous.stdout.strip()}..{tag}" if previous.returncode == 0 else tag
    log = _git(repository, "log", "--no-merges", "-z", "--format=%B", revisions)
    if log.returncode != 0:
        raise RuntimeError(f"git log {revisions} failed: {log.stderr.strip()}")
    subjects = []
    for message in filter(str.strip, log.stdout.split("\0")):
        subject, *body = message.strip().splitlines()
        listed = [
            line[2:].strip()
            for line in body
            if line.startswith("- ") and CONVENTIONAL_SUBJECT.fullmatch(line[2:].strip())
        ]
        subjects += listed or [subject]
    return subjects


def release_notes(subjects: Iterable[str]) -> str:
    """The changes users get, grouped by the part of Bokucho they touch."""
    entries: dict[str, list[str]] = {section: [] for section in SECTIONS}
    for subject in subjects:
        match = CONVENTIONAL_SUBJECT.fullmatch(subject)
        if match is None or match["type"] not in MARKERS:
            continue
        scope = match["scope"]
        description = match["description"]
        label = f"[{scope}] " if scope else ""
        entries[SECTION_OF_SCOPE.get(scope, CORE)].append(
            f"{MARKERS[match['type']]} {label}{description[0].upper()}{description[1:]}"
        )
    return "".join(
        f"{section}\n" + "".join(f"{line}\n" for line in lines) + "\n"
        for section, lines in entries.items()
        if lines
    )


def write_checksums(files: Sequence[Path], directory: Path) -> list[Path]:
    """One ``sha256sum``-style file per algorithm, so a download checks with ``-c``."""
    written = []
    for name, algorithm in CHECKSUM_FILES.items():
        lines = []
        for path in files:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, algorithm).hexdigest()
            lines.append(f"{digest}  {path.name}\n")
        target = directory / name
        target.write_text("".join(lines), encoding="ascii", newline="\n")
        written.append(target)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="The release tag, such as v0.1.0.")
    arguments = parser.parse_args()
    if not PORTABLE_ZIP.is_file():
        raise SystemExit(f"No release archive at {PORTABLE_ZIP}; build it first.")
    NOTES.write_text(release_notes(commit_subjects(arguments.tag)), encoding="utf-8", newline="\n")
    print(f"Wrote {NOTES}")
    for path in write_checksums([PORTABLE_ZIP], DIST_ROOT):
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
