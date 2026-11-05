"""Stage the fonts Bokucho ships for typesetting pictures."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = ROOT / "fonts"
DEFAULT_CACHE = ROOT / "build" / "font-cache"
USER_AGENT = "Bokucho-font-staging/1"

Downloader = Callable[[str, Path], None]


@dataclass(frozen=True)
class ShippedFont:
    name: str
    url: str
    sha256: str
    member: str | None
    filename: str
    license: str


SHIPPED_FONTS: tuple[ShippedFont, ...] = (
    ShippedFont(
        name="GenYoGothic2 TW",
        url="https://github.com/ButTaiwan/genyog-font/releases/download/v2.100/GenYoGothic2TW-otf.zip",
        sha256="616be26e3448b1396b342533d046d8bb0d5b1801d80b5a8ee882811bdc625d7c",
        member="GenYoGothic2TW-R.otf",
        filename="GenYoGothic2TW-R.otf",
        license="GenYo-GenWan-OFL.txt",
    ),
    ShippedFont(
        name="GenYoMin2 TW",
        url="https://github.com/ButTaiwan/genyo-font/releases/download/v2.100/GenYoMin2TW-otf.zip",
        sha256="64ed21a2ef3b42171bdded55a705b32cc105740436c9f61c3d6efec95a769718",
        member="GenYoMin2TW-R.otf",
        filename="GenYoMin2TW-R.otf",
        license="GenYo-GenWan-OFL.txt",
    ),
    ShippedFont(
        name="GenWanMin2 TW",
        url="https://github.com/ButTaiwan/genwan-font/releases/download/v2.100/GenWanMin2TW-otf.zip",
        sha256="ddc324b2f3f804e36b6084b2e773900a8f74455c1fbfa1f38d082344f4ab3264",
        member="GenWanMin2TW-R.otf",
        filename="GenWanMin2TW-R.otf",
        license="GenYo-GenWan-OFL.txt",
    ),
    ShippedFont(
        name="Hanazono Mincho A",
        url="https://glyphwiki.org/hanazono/hanazono-20170904.zip",
        sha256="571cd4a09ae7da0c642d640fc2442c050aa450ebb0587a95cdd097d41a9c9572",
        member="HanaMinA.ttf",
        filename="HanaMinA.ttf",
        license="Hanazono-License.txt",
    ),
    ShippedFont(
        name="LINE Seed TW",
        url="https://seed.line.me/src/images/fonts/LINE_Seed_TW.zip",
        sha256="a1992cd9e372f94a3cef52b98cee781639fe8b7f4d1a77e318a133acf857aecd",
        member="LINE Seed TW_ver02/TTF/LINESeedTW_TTF_Rg.ttf",
        filename="LINESeedTW_TTF_Rg.ttf",
        license="LINE-Seed-TW-OFL.txt",
    ),
    ShippedFont(
        name="851 Tegaki Zatsu",
        url="https://pm85122.onamae.jp/851tegaki_zatsu_normal_0883.ttf",
        sha256="e5290d6583e1aef725f07921aad69b9eaa83cf3408273cf840ff31842d118f0e",
        member=None,
        filename="851tegaki_zatsu.ttf",
        license="851-Font-Terms.txt",
    ),
    ShippedFont(
        name="GenEi Antique",
        url="https://okoneya.jp/font/GenEiAntique_v6.0a.zip",
        sha256="e8a9df9c1ace0b5234c17a66f35729c9cc17955e8b37dc94c0fa9080f757fe03",
        member="GenEiAntique_v6.0a/GenEiAntiqueNv6-M.ttf",
        filename="GenEiAntiqueN-M.ttf",
        license="GenEi-Antique-OFL.txt",
    ),
    ShippedFont(
        name="jf open huninn",
        url="https://github.com/justfont/open-huninn-font/releases/download/v2.1/jf-openhuninn-2.1.ttf",
        sha256="9d5bf4932d31fe94c18cd8cfddc98bc1b14ce10f4e354c682179db290a99c825",
        member=None,
        filename="jf-openhuninn.ttf",
        license="jf-open-huninn-OFL.txt",
    ),
)

INTERFACE_FONT = ShippedFont(
    name="Noto Sans TC",
    url=(
        "https://raw.githubusercontent.com/notofonts/noto-cjk/"
        "523d033d6cb47f4a80c58a35753646f5c3608a78/Sans/Variable/OTF/Subset/NotoSansTC-VF.otf"
    ),
    sha256="a080726eee9d78ec74fd95184a79c58c3085f10713e26876aab0130f543e9120",
    member=None,
    filename="NotoSansTC-VF.otf",
    license="Noto-Sans-CJK-OFL.txt",
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


def _fetch(font: ShippedFont, cache: Path, downloader: Downloader) -> Path:
    """The download ``font`` comes from, fetched only if no good copy is kept."""
    cached = cache / font.url.rsplit("/", 1)[1]
    if cached.is_file() and _digest(cached) == font.sha256:
        return cached
    cache.mkdir(parents=True, exist_ok=True)
    partial = cached.with_name(cached.name + ".partial")
    downloader(font.url, partial)
    actual = _digest(partial)
    if actual != font.sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{font.url} does not match its pinned hash; it served {actual}")
    partial.replace(cached)
    return cached


def stage_fonts(
    destination: Path,
    cache: Path = DEFAULT_CACHE,
    *,
    downloader: Downloader = _download,
    fonts: Sequence[ShippedFont] = SHIPPED_FONTS,
) -> list[Path]:
    """Fill ``destination`` with every shipped face and nothing else."""
    downloads = [_fetch(font, cache, downloader) for font in fonts]
    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for font, download in zip(fonts, downloads, strict=True):
        target = staging / font.filename
        if font.member is None:
            shutil.copyfile(download, target)
            continue
        with (
            zipfile.ZipFile(download) as archive,
            archive.open(font.member) as face,
            target.open("wb") as handle,
        ):
            shutil.copyfileobj(face, handle)
    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    return [destination / font.filename for font in fonts]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help=f"Folder the faces go in; replaced as a whole (default: {DEFAULT_DESTINATION}).",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"Where downloads are kept between runs (default: {DEFAULT_CACHE}).",
    )
    parser.add_argument(
        "--interface",
        action="store_true",
        help=f"Stage only {INTERFACE_FONT.name}, the face Bokucho's window is lettered in.",
    )
    arguments = parser.parse_args(argv)
    fonts = (INTERFACE_FONT,) if arguments.interface else SHIPPED_FONTS
    faces = stage_fonts(arguments.destination, arguments.cache, fonts=fonts)
    size = sum(face.stat().st_size for face in faces)
    print(f"Staged {len(faces)} fonts ({size / 1_000_000:.0f} MB) -> {arguments.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
