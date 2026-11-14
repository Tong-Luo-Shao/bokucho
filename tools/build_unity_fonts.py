"""Build the TextMesh Pro fallback fonts a Unity patch carries, one per Unity generation.

A TMP font asset only loads in a game whose Unity reads its bundle, so each
generation is built in its own Unity Editor (installed through Unity Hub). The
face is GenYoGothic2 TW, the OFL face Bokucho already ships, with every
character it maps in the Basic Multilingual Plane.

Developers run this once per font change; players never need Unity.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_typeset_fonts import SHIPPED_FONTS, _download, _fetch  # noqa: E402

from bokucho.engines.unity import font_bundle  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / "build" / "unity-fonts"
DEFAULT_WORK = ROOT / "build" / "unity-font-work"
DEFAULT_EDITORS = Path(r"C:\Program Files\Unity\Hub\Editor")
BUILDER = Path(__file__).resolve().parent / "unity_fonts" / "BokuchoFontBuilder.cs"
FACE = next(font for font in SHIPPED_FONTS if font.name == "GenYoGothic2 TW")
TMP_PACKAGE = "com.unity.textmeshpro"
ESSENTIALS = "Package Resources/TMP Essential Resources.unitypackage"
# Characters TMP cannot draw or that only steer layout.
_SKIPPED_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Zl", "Zp"}


@dataclass(frozen=True)
class Generation:
    name: str
    editor: str
    tmp: str | None  # None: TMP ships inside com.unity.ugui (Unity 6)
    # Editors before 2020 license through a file Unity Hub no longer writes; opened with a
    # window they take the license from the signed-in Hub instead.
    batch: bool = True


GENERATIONS = (
    Generation("u2018", "2018.4.", "1.4.1", batch=False),
    Generation("u2019", "2019.2.", "2.0.1", batch=False),
    Generation("u2021", "2021.3.", "3.0.6"),
    Generation("u2022", "2022.3.", "3.0.6"),
    Generation("u6000", "6000.", None),
)


def characters(cmap: dict[int, str]) -> str:
    """Every drawable Basic Multilingual Plane character the face maps, in two lines.

    The first line holds one character per glyph and the second the characters whose
    glyph the first already brings: TMP 2.0 throws when one call adds a glyph twice.
    """
    # shortcut: older TMP adds a string one UTF-16 unit at a time, so supplementary
    # characters (CJK Extension B and up) are left out; add them per generation if needed.
    first: list[str] = []
    shared: list[str] = []
    seen: set[str] = set()
    for code in sorted(cmap):
        if code >= 0x10000 or unicodedata.category(chr(code)) in _SKIPPED_CATEGORIES:
            continue
        (shared if cmap[code] in seen else first).append(chr(code))
        seen.add(cmap[code])
    return "".join(first) + "\n" + "".join(shared)


def find_editor(editors: Path, prefix: str) -> Path:
    """The newest installed ``Unity.exe`` whose version starts with ``prefix``."""

    def version(path: Path) -> tuple:
        return tuple(
            int(part) if part.isdigit() else part for part in path.name.replace("f", ".").split(".")
        )

    found = sorted(
        (path for path in editors.glob(f"{prefix}*") if (path / "Editor" / "Unity.exe").is_file()),
        key=version,
    )
    if not found:
        raise RuntimeError(f"No Unity {prefix}x editor under {editors}; install one with Unity Hub")
    return found[-1] / "Editor" / "Unity.exe"


def with_text_mesh_pro(manifest: str, tmp: str | None) -> str:
    """A package manifest that brings TextMesh Pro, keeping a version already there."""
    data = json.loads(manifest)
    dependencies = data.setdefault("dependencies", {})
    if tmp is None:
        dependencies.setdefault("com.unity.ugui", "2.0.0")
    else:
        dependencies.setdefault(TMP_PACKAGE, tmp)
    return json.dumps(data, indent=2) + "\n"


def unpack_unitypackage(package: Path, project: Path) -> None:
    """Write a .unitypackage's assets into ``project``, as Unity's own import would.

    Batch-mode ``AssetDatabase.ImportPackage`` returns before importing anything.
    """
    entries: dict[str, dict[str, bytes]] = {}
    with tarfile.open(package, "r:gz") as archive:
        for member in archive.getmembers():
            parts = PurePosixPath(member.name.removeprefix("./")).parts
            if member.isfile() and len(parts) == 2:
                data = archive.extractfile(member)
                if data is not None:
                    entries.setdefault(parts[0], {})[parts[1]] = data.read()
    for entry in entries.values():
        if "pathname" not in entry or "asset" not in entry:
            continue  # a folder: Unity makes it when it imports what is inside
        name = entry["pathname"].decode("utf-8").splitlines()[0].strip()
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "Assets":
            raise RuntimeError(f"{package.name} holds an unsafe path: {name}")
        target = project.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry["asset"])
        if "asset.meta" in entry:
            target.with_name(target.name + ".meta").write_bytes(entry["asset.meta"])


def _face(work: Path) -> Path:
    archive = _fetch(FACE, work / "cache", _download)
    face = work / FACE.filename
    with zipfile.ZipFile(archive) as source, source.open(FACE.member) as data:
        face.write_bytes(data.read())
    return face


def _run(command: list[str], log: Path, timeout: float | None = None) -> None:
    result = subprocess.run([*command, "-logFile", str(log)], check=False, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"Unity exited with {result.returncode}; see {log}")


def _fill(project: Path, face: Path, unity: Path) -> None:
    # A project without its editor's version makes a windowed editor stop and ask.
    settings = project / "ProjectSettings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "ProjectVersion.txt").write_text(
        f"m_EditorVersion: {unity.parent.parent.name}\n", encoding="utf-8"
    )
    (project / "Assets" / "Editor").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUILDER, project / "Assets" / "Editor" / BUILDER.name)
    shutil.copyfile(face, project / "Assets" / face.name)


def _unpack_essentials(resolved: Path, project: Path) -> None:
    essentials = sorted((resolved / "Library" / "PackageCache").glob(f"*/{ESSENTIALS}"))
    if not essentials:
        raise RuntimeError(f"{resolved} has no {ESSENTIALS} after resolving its packages")
    unpack_unitypackage(essentials[0], project)


def build_generation(
    generation: Generation, editors: Path, face: Path, chars: Path, work: Path, destination: Path
) -> Path:
    unity = find_editor(editors, generation.editor)
    mode = ["-batchmode"] if generation.batch else []
    project = work / generation.name
    if project.exists():
        shutil.rmtree(project)
    if generation.batch:
        _run(
            [str(unity), *mode, "-quit", "-createProject", str(project)],
            work / f"{generation.name}-create.log",
        )
        manifest = project / "Packages" / "manifest.json"
        manifest.write_text(
            with_text_mesh_pro(manifest.read_text(encoding="utf-8"), generation.tmp),
            encoding="utf-8",
        )
        _fill(project, face, unity)
        # One launch to fetch TextMesh Pro, whose shaders then go in before the build launch.
        _run(
            [str(unity), *mode, "-quit", "-projectPath", str(project)],
            work / f"{generation.name}-resolve.log",
        )
        _unpack_essentials(project, project)
        opened = ["-projectPath", str(project)]
    else:
        # A legacy editor with a window quits at once on an existing project, so the
        # template project only lends its TMP shaders to a filled one created and built
        # in a single launch.
        _fill(project, face, unity)
        try:
            _run(
                [
                    str(unity),
                    "-createProject",
                    str(project),
                    "-executeMethod",
                    "BokuchoFontBuilder.Quit",
                ],
                work / f"{generation.name}-create.log",
                timeout=900,
            )
        except (RuntimeError, subprocess.TimeoutExpired):
            pass  # it can hang on the way out; the packages it fetched are checked next
        built_project = work / f"{generation.name}-build"
        if built_project.exists():
            shutil.rmtree(built_project)
        _fill(built_project, face, unity)
        _unpack_essentials(project, built_project)
        opened = ["-createProject", str(built_project)]
    output = work / f"{generation.name}-out"
    name = font_bundle(generation.name)
    _run(
        [
            str(unity),
            *mode,
            *opened,
            "-executeMethod",
            "BokuchoFontBuilder.Build",
            "-bokuchoFont",
            f"Assets/{face.name}",
            "-bokuchoChars",
            str(chars),
            "-bokuchoBundle",
            name,
            "-bokuchoOut",
            str(output),
        ],
        work / f"{generation.name}-build.log",
    )
    destination.mkdir(parents=True, exist_ok=True)
    built = destination / name
    shutil.copyfile(output / name, built)
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--editors", type=Path, default=DEFAULT_EDITORS)
    parser.add_argument("--only", action="append", help="Build just these generations (e.g. u2019)")
    arguments = parser.parse_args(argv)
    from fontTools.ttLib import TTFont

    arguments.work.mkdir(parents=True, exist_ok=True)
    face = _face(arguments.work)
    chars = arguments.work / "characters.txt"
    chars.write_text(characters(TTFont(face).getBestCmap()), encoding="utf-8")
    for generation in GENERATIONS:
        if arguments.only and generation.name not in arguments.only:
            continue
        built = build_generation(
            generation, arguments.editors, face, chars, arguments.work, arguments.destination
        )
        print(f"{generation.name}: {built} ({built.stat().st_size / 1_000_000:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
