"""Build the current pywebview GUI as a single portable Windows executable."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_ROOT = ROOT / "build" / "pyinstaller"
STAGING_DIST = BUILD_ROOT / "dist"
WORK_PATH = BUILD_ROOT / "work"
SPEC_PATH = BUILD_ROOT / "spec"
DIST_ROOT = ROOT / "dist"
PORTABLE_DIR = DIST_ROOT / "Bokucho-Windows-x64"
PORTABLE_ZIP = DIST_ROOT / "Bokucho-Windows-x64.zip"
OCR_BOOTSTRAP_TOOL = ROOT / "tools" / "fetch_ocr_runtime.py"
TYPESET_FONTS_TOOL = ROOT / "tools" / "fetch_typeset_fonts.py"
INTERFACE_FONT_DIR = BUILD_ROOT / "interface-font"
PATCHER_ENTRY = ROOT / "src" / "bokucho" / "patcher" / "__main__.py"
PATCHER_DIST = ROOT / "build" / "patcher"
PATCHER_NAME = "bokucho-patcher"
PATCHER_STUB = PATCHER_DIST / f"{PATCHER_NAME}.exe"
UIF_REPOSITORY = "https://github.com/AtomCrafty/UniversalInjectorFramework.git"
UIF_COMMIT = "7ad6bddb566fbfdab60b7e45bffd4af920c2b559"
UIF_SOURCE = ROOT / "build" / "uif-source"
UIF_PROPS = ROOT / "tools" / "uif.props"
UIF_PATCH = ROOT / "tools" / "uif-font-path.patch"
UIF_DIST = ROOT / "build" / "uif"
UIF_DLL = UIF_DIST / "winmm.dll"
UIF_TOOLSET = "v143"

HIDDEN_IMPORTS = (
    "bokucho.engines.livemaker",
    "bokucho.engines.renpy",
    "bokucho.engines.rpgmaker",
    "bokucho.engines.rpgmaker_marshal",
    "bokucho.engines.rpgmaker_scripts",
    "bokucho.engines.ruby_literals",
    "bokucho.engines.rgssad",
    "bokucho.translators.claude",
    "bokucho.translators.local",
    "bokucho.transform.opencc",
    "bokucho.transform.punctuation",
    "tiktoken_ext.openai_public",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
)

EXCLUDED_MODULES = (
    "accelerate",
    "cefpython3",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "torch",
    "torchvision",
    "transformers",
)

PORTABLE_DOCUMENTS = (
    "LICENSE",
    "PORTABLE_README.md",
    "THIRD_PARTY_NOTICES.md",
)
PORTABLE_DIRECTORIES = ("licenses",)


def _remove_path(path: Path, allowed_parent: Path) -> None:
    resolved = path.resolve()
    parent = allowed_parent.resolve()
    relative = resolved.relative_to(parent)
    if relative == Path("."):
        raise RuntimeError(f"Refusing to remove build root: {resolved}")
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.exists():
        resolved.unlink()


def pyinstaller_arguments(
    patcher: Path = PATCHER_STUB,
    uif: Path = UIF_DLL,
    interface_font: Path = INTERFACE_FONT_DIR,
) -> list[str]:
    source_root = ROOT / "src"
    entrypoint = ROOT / "src" / "main.py"
    web_assets = source_root / "bokucho" / "web"
    resources = source_root / "bokucho" / "resources"
    arguments = [
        str(entrypoint),
        "--clean",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name=Bokucho",
        f"--paths={source_root}",
        f"--distpath={STAGING_DIST}",
        f"--workpath={WORK_PATH}",
        f"--specpath={SPEC_PATH}",
        f"--add-data={web_assets}{os.pathsep}bokucho/web",
        f"--add-data={resources}{os.pathsep}bokucho/resources",
        f"--add-data={patcher}{os.pathsep}bokucho/patcher",
        f"--add-data={uif}{os.pathsep}bokucho/uif",
        f"--add-data={interface_font}{os.pathsep}bokucho/web/assets/fonts",
        "--collect-binaries=webview",
        "--collect-data=webview",
    ]
    arguments.extend(f"--hidden-import={module}" for module in HIDDEN_IMPORTS)
    arguments.extend(f"--exclude-module={module}" for module in EXCLUDED_MODULES)
    return arguments


def patcher_arguments() -> list[str]:
    arguments = [
        str(PATCHER_ENTRY),
        "--clean",
        "--noconfirm",
        "--onefile",
        "--windowed",
        f"--name={PATCHER_NAME}",
        f"--paths={ROOT / 'src'}",
        f"--distpath={PATCHER_DIST}",
        f"--workpath={BUILD_ROOT / 'patcher-work'}",
        f"--specpath={BUILD_ROOT / 'patcher-spec'}",
    ]
    if sys.version_info < (3, 14):
        arguments.append("--hidden-import=backports.zstd")
    arguments.extend(f"--exclude-module={module}" for module in (*EXCLUDED_MODULES, "lingua"))
    return arguments


def build_patcher() -> Path:
    """Freeze the player's patcher: the program every patch executable starts as."""
    if sys.platform != "win32":
        raise RuntimeError("The patcher must be built on Windows.")

    import PyInstaller.__main__

    _remove_path(PATCHER_DIST, ROOT / "build")
    PyInstaller.__main__.run(patcher_arguments())
    if not PATCHER_STUB.is_file():
        raise RuntimeError(f"PyInstaller did not produce {PATCHER_STUB}")
    check_patcher(PATCHER_STUB)
    return PATCHER_STUB


def check_patcher(stub: Path) -> None:
    """Append a small patch to the frozen patcher and apply it without the window."""
    import hashlib
    import tempfile

    from bokucho.patcher.delta import make_delta
    from bokucho.patcher.payload import FileState, Patch, PatchedFile, patch_executable

    original = b"original game " * 4096
    translated = b"translated game " * 4096
    delta, window = make_delta(original, translated)

    def state(data: bytes) -> FileState:
        return FileState(len(data), hashlib.sha256(data).hexdigest())

    patch = Patch(
        "check", (PatchedFile("Game.exe", state(original), state(translated), delta, window),)
    )
    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch) / "ゲーム"
        folder.mkdir()
        (folder / "Game.exe").write_bytes(original)
        executable = Path(scratch) / "check-patch.exe"
        executable.write_bytes(patch_executable(stub.read_bytes(), patch))
        completed = subprocess.run(
            [str(executable), "--apply", str(folder)], timeout=300, check=False
        )
        if completed.returncode != 0 or (folder / "Game.exe").read_bytes() != translated:
            raise RuntimeError(
                f"The frozen patcher did not apply a patch (exit {completed.returncode})."
            )
    check_vx_ace_patcher(stub)


def check_vx_ace_patcher(stub: Path) -> None:
    """Compile a VX Ace recipe and a loose XP one with a script literal in the frozen patcher."""
    import tempfile
    import zlib

    from rubymarshal.classes import RubyObject, RubyString

    from bokucho.application.projects import CreateProjectRequest, create_project
    from bokucho.application.release import build_patch
    from bokucho.engines.rpgmaker import (
        DATA_SUFFIX,
        VARIANT_ENGINES,
        RpgMakerAdapter,
        Variant,
        file_slots,
        load_data,
    )
    from bokucho.engines.rpgmaker_marshal import dump_marshal
    from bokucho.engines.rpgmaker_scripts import load_scripts
    from bokucho.model import StringFile
    from bokucho.project import Project

    def actors(name: object) -> bytes:
        actor = RubyObject("RPG::Actor")
        actor.attributes = {"@id": 1, "@name": name}
        return dump_marshal([None, actor])

    name = RubyString("ハロルド")
    name.attributes = {"E": True}
    script = dump_marshal([[0, b"Scene_End", zlib.compress(b's1 = "To Title"\n')]])
    cases = (
        (Variant.ACE, {"Actors": actors(name)}, []),
        (
            Variant.XP,
            {"Actors": actors("ハロルド".encode()), "Scripts": script},
            ['s1 = "回到標題"\n'],
        ),
    )
    targets = {"ハロルド": "哈羅德", "To Title": "回到標題"}
    with tempfile.TemporaryDirectory() as scratch:
        for variant, files, sources in cases:
            suffix = DATA_SUFFIX[variant]
            game = Path(scratch) / variant / "ゲーム"
            data = game / "Data"
            data.mkdir(parents=True)
            (data / f"System{suffix}").write_bytes(dump_marshal(RubyObject("RPG::System")))
            for stem, blob in files.items():
                (data / f"{stem}{suffix}").write_bytes(blob)
            project = create_project(
                CreateProjectRequest(
                    Path(scratch) / variant / "project", "check", "ja", "zh-tw", "rpgmaker"
                )
            )
            engine = project.config.engine.model_copy(update={"game_path": str(game)})
            project.save_config(project.config.model_copy(update={"engine": engine}))
            units = RpgMakerAdapter().extract(game)
            for unit in units:
                unit.target = targets[unit.source]
            project.save_string_file(StringFile(source_file="Actors", units=units), "actors.json")
            executable = build_patch(
                Project.load(project.root),
                adapter_factory=lambda current: RpgMakerAdapter(),
                stub=stub,
            ).executable
            completed = subprocess.run(
                [str(executable), "--apply", str(game)], timeout=300, check=False
            )
            names = [
                slot.text
                for slot in file_slots("Actors", load_data(data / f"Actors{suffix}"), variant)
            ]
            scripts = data / f"Scripts{suffix}"
            found = (
                [entry.source for entry in load_scripts(scripts.read_bytes()).entries]
                if scripts.is_file()
                else []
            )
            if completed.returncode != 0 or names != ["哈羅德"] or found != sources:
                raise RuntimeError(
                    f"The frozen patcher did not compile a {VARIANT_ENGINES[variant]} patch "
                    f"(exit {completed.returncode})."
                )


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=UIF_SOURCE, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def fetch_uif() -> Path:
    """UIF's source at UIF_COMMIT with UIF_PATCH applied; the clone is fetched once."""
    head = ""
    if (UIF_SOURCE / ".git").is_dir():
        try:
            head = _git("rev-parse", "HEAD")
        except subprocess.CalledProcessError:
            pass
    if head != UIF_COMMIT:
        _remove_path(UIF_SOURCE, ROOT / "build")
        UIF_SOURCE.mkdir(parents=True)
        _git("init", "--quiet")
        _git("fetch", "--quiet", "--depth=1", UIF_REPOSITORY, UIF_COMMIT)
        _git("checkout", "--quiet", "FETCH_HEAD")
        if _git("rev-parse", "HEAD") != UIF_COMMIT:
            raise RuntimeError(f"UIF's source at {UIF_SOURCE} is not commit {UIF_COMMIT}.")
    _git("checkout", "--quiet", "--", ".")
    subprocess.run(
        ["git", "apply", "-"],
        cwd=UIF_SOURCE,
        input=UIF_PATCH.read_bytes().replace(b"\r\n", b"\n"),
        check=True,
        capture_output=True,
    )
    return UIF_SOURCE


def msbuild() -> Path:
    """MSBuild from the newest Visual Studio that has UIF's toolset for 32-bit."""
    program_files = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = program_files / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    installs: list[str] = []
    if vswhere.is_file():
        listed = subprocess.run(
            [str(vswhere), "-all", "-products", "*", "-sort", "-property", "installationPath"],
            check=True,
            capture_output=True,
            text=True,
        )
        installs = listed.stdout.splitlines()
    for install in map(Path, installs):
        toolsets = install / "MSBuild" / "Microsoft" / "VC"
        executable = install / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
        if executable.is_file() and any(
            toolsets.glob(f"*/Platforms/Win32/PlatformToolsets/{UIF_TOOLSET}")
        ):
            return executable
    raise RuntimeError(
        f"Building UIF needs Visual Studio with the {UIF_TOOLSET} C++ toolset "
        "(Visual Studio 2022, Desktop development with C++)."
    )


def build_uif() -> Path:
    """Compile UIF's winmm.dll, which loads a proxy font into the game, from UIF_COMMIT."""
    if sys.platform != "win32":
        raise RuntimeError("UIF must be built on Windows.")
    source = fetch_uif()
    subprocess.run(
        [
            str(msbuild()),
            str(source / "src" / "UniversalInjectorFramework.sln"),
            "-t:UniversalInjectorFramework:Rebuild",
            "-p:Configuration=winmm",
            "-p:Platform=x86",
            f"-p:ForceImportBeforeCppTargets={UIF_PROPS}",
            "-m",
            "-nologo",
            "-verbosity:minimal",
        ],
        check=True,
    )
    output = source / "src" / "Build" / "UniversalInjectorFramework" / "Win32" / "winmm"
    built = output / "winmm.dll"
    if not built.is_file():
        raise RuntimeError(f"MSBuild did not produce {built}")
    _remove_path(UIF_DIST, ROOT / "build")
    UIF_DIST.mkdir(parents=True)
    shutil.copy2(built, UIF_DLL)
    check_uif(UIF_DLL)
    return UIF_DLL


def check_uif(dll: Path) -> None:
    """Refuse a winmm.dll a player's LiveMaker game could not run with."""
    data = dll.read_bytes()
    header = int.from_bytes(data[0x3C:0x40], "little") if data[:2] == b"MZ" else 0
    if not header or data[header : header + 4] != b"PE\0\0":
        raise RuntimeError(f"{dll} is not a Windows DLL.")
    if int.from_bytes(data[header + 4 : header + 6], "little") != 0x14C:
        raise RuntimeError(f"{dll} is not 32-bit; LiveMaker games are.")
    lowered = data.lower()
    for runtime in (b"vcruntime140", b"msvcp140", b"api-ms-win-crt-"):
        if runtime in lowered:
            raise RuntimeError(f"{dll} needs the Visual C++ runtime ({runtime.decode()}).")
    if b"uif_log.log" in data:
        raise RuntimeError(f"{dll} writes uif_log.log beside the game.")


def _remove_archive_outputs() -> None:
    _remove_path(PORTABLE_ZIP, DIST_ROOT)
    for part in PORTABLE_ZIP.parent.glob(f"{PORTABLE_ZIP.name}.*"):
        _remove_path(part, DIST_ROOT)
    _remove_path(DIST_ROOT / "reassemble_release.ps1", DIST_ROOT)


def stage_ocr_bootstrap() -> None:
    subprocess.run(
        [
            sys.executable,
            str(OCR_BOOTSTRAP_TOOL),
            "--release-dir",
            str(PORTABLE_DIR),
        ],
        check=True,
        cwd=ROOT,
    )


def stage_interface_font() -> Path:
    """Fetch the face Bokucho's window is lettered in, for the executable to hold."""
    subprocess.run(
        [
            sys.executable,
            str(TYPESET_FONTS_TOOL),
            "--interface",
            "--destination",
            str(INTERFACE_FONT_DIR),
        ],
        check=True,
        cwd=ROOT,
    )
    return INTERFACE_FONT_DIR


def stage_typeset_fonts() -> None:
    """Put the fonts pictures can be typeset in beside the executable."""
    subprocess.run(
        [
            sys.executable,
            str(TYPESET_FONTS_TOOL),
            "--destination",
            str(PORTABLE_DIR / "fonts"),
        ],
        check=True,
        cwd=ROOT,
    )


def build(
    *,
    app_only: bool = False,
) -> tuple[Path, Path]:
    if sys.platform != "win32":
        raise RuntimeError("The portable Bokucho GUI must be built on Windows.")

    import PyInstaller.__main__

    for path, parent in (
        (BUILD_ROOT, ROOT / "build"),
        (PORTABLE_DIR, DIST_ROOT),
    ):
        _remove_path(path, parent)
    _remove_archive_outputs()

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    stub = build_patcher()
    uif = build_uif()
    interface_font = stage_interface_font()
    PyInstaller.__main__.run(pyinstaller_arguments(stub, uif, interface_font))

    staged = STAGING_DIST / "Bokucho.exe"
    if not staged.is_file():
        raise RuntimeError(f"PyInstaller did not produce {staged}")

    PORTABLE_DIR.mkdir(parents=True)
    (PORTABLE_DIR / "Resource").mkdir()
    shutil.move(str(staged), str(PORTABLE_DIR / "Bokucho.exe"))
    for name in PORTABLE_DOCUMENTS:
        shutil.copy2(ROOT / name, PORTABLE_DIR / name)
    for name in PORTABLE_DIRECTORIES:
        shutil.copytree(ROOT / name, PORTABLE_DIR / name)

    if not app_only:
        stage_ocr_bootstrap()
        stage_typeset_fonts()
    archived = archive()
    return PORTABLE_DIR / "Bokucho.exe", archived


def archive() -> Path:
    """Archive the current portable folder as one ZIP."""
    if not PORTABLE_DIR.is_dir():
        raise RuntimeError(f"No portable folder at {PORTABLE_DIR}; build it first.")
    _remove_archive_outputs()
    shutil.make_archive(
        str(PORTABLE_ZIP.with_suffix("")),
        "zip",
        root_dir=DIST_ROOT,
        base_dir=PORTABLE_DIR.name,
    )
    return PORTABLE_ZIP


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-only",
        action="store_true",
        help="Re-zip the existing portable folder, staged extras included.",
    )
    parser.add_argument(
        "--app-only",
        action="store_true",
        help="Build the lightweight application without the OCR bootstrap or the fonts.",
    )
    parser.add_argument(
        "--patcher-only",
        action="store_true",
        help="Build only the patcher program a release appends each patch to.",
    )
    parser.add_argument(
        "--uif-only",
        action="store_true",
        help="Build only UIF's winmm.dll, which a release puts beside a game for its proxy font.",
    )
    arguments = parser.parse_args()
    if arguments.uif_only:
        print(f"Built {build_uif()}")
        return
    if arguments.patcher_only:
        print(f"Built {build_patcher()}")
        return
    if arguments.archive_only:
        print(f"Archived {archive()}")
        return
    executable, archived = build(app_only=arguments.app_only)
    print(f"Built {executable}")
    print(f"Built {archived}")


if __name__ == "__main__":
    main()
