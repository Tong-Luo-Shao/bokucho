"""Stage the BepInEx loaders and XUnity.AutoTranslator a Unity patch puts beside the game.

Mono games get BepInEx 5 (``<x64|x86>``); IL2CPP games get BepInEx 6 with the
IL2CPP build of XUnity (``il2cpp-<x64|x86>``). Every archive is pinned by its
SHA-256. An archive whose pin is still empty is skipped, and a patch for a game
that needs it is refused; ``--print-pins`` fetches such archives once and prints
the digests to review and paste below.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / "build" / "unity-runtime"
DEFAULT_CACHE = ROOT / "build" / "unity-cache"
DEFAULT_FONTS = ROOT / "build" / "unity-fonts"  # tools/build_unity_fonts.py writes these
LICENSES = ROOT / "licenses"
#: Where a patch carries the terms of what it hands the player: the archives hold none,
#: and BepInEx and Unity Doorstop (LGPL-2.1) and Il2CppInterop (LGPL-3.0) ask for them.
LICENSE_DIR = ("BepInEx", "licenses")
MONO_LICENSES = (
    "BepInEx-LGPL-2.1.txt",
    "UnityDoorstop-LGPL-2.1.txt",
    "XUnity.AutoTranslator-MIT.txt",
)
#: LGPL-3.0 builds on the GPL-3.0, so its text travels with it.
IL2CPP_LICENSES = (
    *MONO_LICENSES,
    "Il2CppInterop-LGPL-3.0.txt",
    "Cpp2IL-MIT.txt",
    "dotnet-runtime-MIT.txt",
)
GPL = ROOT / "LICENSE"
USER_AGENT = "Bokucho-unity-staging/1"

Downloader = Callable[[str, Path], None]


@dataclass(frozen=True)
class Download:
    url: str
    sha256: str  # "" until a maintainer has reviewed the archive and pinned it

    @property
    def pinned(self) -> bool:
        return len(self.sha256) == 64


BEPINEX = {
    "x64": Download(
        "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.5/BepInEx_win_x64_5.4.23.5.zip",
        "82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4",
    ),
    "x86": Download(
        "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.5/BepInEx_win_x86_5.4.23.5.zip",
        "37651c79e40d6f909572a4f461ac25350bb3ef8fe7fbd29f1aa8791a33b84c82",
    ),
}
XUNITY = Download(
    "https://github.com/bbepis/XUnity.AutoTranslator/releases/download/v5.6.3.1/"
    "XUnity.AutoTranslator-BepInEx-5.6.3.1.zip",
    "6c3ff03aafe9708f25c2115c0c04c255b6a9d88e716896ae7bacb590a8777609",
)
# BepInEx 6 is the only BepInEx that loads into IL2CPP games. On a game's first
# start it fetches that Unity version's base libraries from unity.bepinex.dev and
# generates its interop assemblies, so that start needs a network and takes longer.
# Pinned from the archives --print-pins fetched; each holds winhttp.dll, BepInEx/ and dotnet/.
BEPINEX_IL2CPP = {
    "x64": Download(
        "https://github.com/BepInEx/BepInEx/releases/download/v6.0.0-pre.2/"
        "BepInEx-Unity.IL2CPP-win-x64-6.0.0-pre.2.zip",
        "616ec7eb06cf11b2a0000e8fcef04d1b12bb58e84a2e0bdac9523234fc193ceb",
    ),
    "x86": Download(
        "https://github.com/BepInEx/BepInEx/releases/download/v6.0.0-pre.2/"
        "BepInEx-Unity.IL2CPP-win-x86-6.0.0-pre.2.zip",
        "cfef3a1e946dac5db8b9de4de1a922f47584dd775da32863f36762fbaad80f19",
    ),
}
XUNITY_IL2CPP = Download(
    "https://github.com/bbepis/XUnity.AutoTranslator/releases/download/v5.6.3.1/"
    "XUnity.AutoTranslator-BepInEx-IL2CPP-5.6.3.1.zip",
    "f1d4d3151a061edfcda815e4472fe1f1de8756bdc23708c9b5ebb9bfe736fb1f",
)


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(download: Download, cache: Path, downloader: Downloader) -> Path:
    """The pinned archive, fetched only if no good copy is kept."""
    if not download.pinned:
        raise RuntimeError(f"{download.url} has no pinned hash; run --print-pins and pin it")
    cached = cache / download.url.rsplit("/", 1)[1]
    if cached.is_file() and _digest(cached) == download.sha256:
        return cached
    cache.mkdir(parents=True, exist_ok=True)
    partial = cached.with_name(cached.name + ".partial")
    downloader(download.url, partial)
    actual = _digest(partial)
    if actual != download.sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{download.url} does not match its pinned hash; it served {actual}")
    partial.replace(cached)
    return cached


def _extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            name = PurePosixPath(info.filename)
            if (
                name.is_absolute()
                or ".." in name.parts
                or ":" in info.filename
                or "\\" in info.filename
            ):
                raise RuntimeError(f"{archive.name} holds an unsafe path: {info.filename}")
            if not info.is_dir():
                target = destination.joinpath(*name.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as data, target.open("wb") as handle:
                    shutil.copyfileobj(data, handle)


def _stage_loaders(
    staging: Path,
    prefix: str,
    loaders: dict[str, Download],
    plugin: Download,
    cache: Path,
    downloader: Downloader,
    licenses: tuple[str, ...],
) -> None:
    xunity = _fetch(plugin, cache, downloader)
    for architecture, download in loaders.items():
        tree = staging / f"{prefix}{architecture}"
        tree.mkdir(parents=True)
        _extract(_fetch(download, cache, downloader), tree)
        _extract(xunity, tree)
        if not (tree / "winhttp.dll").is_file():
            raise RuntimeError(f"The BepInEx {prefix}{architecture} archive has no winhttp.dll")
        _stage_licenses(tree, licenses)


def _stage_licenses(tree: Path, licenses: tuple[str, ...]) -> None:
    folder = tree.joinpath(*LICENSE_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    for name in licenses:
        shutil.copyfile(LICENSES / name, folder / name)
    if any("LGPL-3.0" in name for name in licenses):
        shutil.copyfile(GPL, folder / "GPL-3.0.txt")


def il2cpp_pinned() -> bool:
    """Whether every IL2CPP archive has a reviewed pin."""
    return XUNITY_IL2CPP.pinned and all(item.pinned for item in BEPINEX_IL2CPP.values())


def stage_runtime(
    destination: Path = DEFAULT_DESTINATION,
    cache: Path = DEFAULT_CACHE,
    *,
    downloader: Downloader = _download,
    fonts: Path = DEFAULT_FONTS,
) -> Path:
    """Fill ``destination/<x64|x86>`` (and ``il2cpp-<x64|x86>``) with BepInEx and XUnity.

    The IL2CPP trees are staged only once their archives are pinned. The TextMesh
    Pro fallback fonts in ``fonts``, if built, go in ``destination/fonts``.
    """
    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    _stage_loaders(staging, "", BEPINEX, XUNITY, cache, downloader, MONO_LICENSES)
    if il2cpp_pinned():
        _stage_loaders(
            staging, "il2cpp-", BEPINEX_IL2CPP, XUNITY_IL2CPP, cache, downloader, IL2CPP_LICENSES
        )
    else:
        print("IL2CPP loaders are not pinned; IL2CPP Unity games will get no patch.")
    if fonts.is_dir():
        shutil.copytree(fonts, staging / "fonts")
    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    return destination


def print_pins(cache: Path = DEFAULT_CACHE, downloader: Downloader = _download) -> dict[str, str]:
    """Fetch each archive that has no pin and return (and print) its SHA-256.

    The digests are for a maintainer to check against the release page and then
    paste into this file; nothing is staged from an unpinned archive.
    """
    found = {}
    for download in (*BEPINEX_IL2CPP.values(), XUNITY_IL2CPP):
        if download.pinned:
            continue
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / (download.url.rsplit("/", 1)[1] + ".unpinned")
        downloader(download.url, target)
        found[download.url] = _digest(target)
        print(f"{found[download.url]}  {download.url}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--print-pins",
        action="store_true",
        help="download the archives that have no pinned hash and print their SHA-256",
    )
    arguments = parser.parse_args(argv)
    if arguments.print_pins:
        if not print_pins(arguments.cache):
            print("Every archive is pinned.")
        return 0
    print(f"Staged {stage_runtime(arguments.destination, arguments.cache)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
