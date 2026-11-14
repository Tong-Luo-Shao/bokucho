"""Draw tools/icon/bokucho.ico, the icon Bokucho.exe shows, from the SVGs beside it.

The .ico is committed and tools/build.py only reads it, so run this again only after
changing an SVG. Edge draws the SVGs, since Pillow cannot and Edge comes with Windows;
Pillow packs the pictures.
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image

ICON_DIR = Path(__file__).resolve().parent / "icon"
ICON = ICON_DIR / "bokucho.ico"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
#: The drawing each size comes from: at 32 px and below the full drawing's fine veins
#: and washes turn grey, so those sizes come from a plainer one.
SOURCES = {
    256: "bokucho.svg",
    64: "bokucho.svg",
    48: "bokucho.svg",
    40: "bokucho.svg",
    32: "bokucho-small.svg",
    24: "bokucho-small.svg",
    20: "bokucho-small.svg",
    16: "bokucho-small.svg",
}
#: Drawn this many times larger, then shrunk: smoother edges than drawing at the size.
OVERSAMPLE = 4


def render(svg: Path, size: int, scratch: Path) -> Image.Image:
    """``svg`` drawn by Edge as a ``size`` px square picture with a clear background."""
    pixels = size * OVERSAMPLE
    page = scratch / f"{svg.stem}-{size}.html"
    page.write_text(
        '<body style="margin:0;background:transparent">'
        f'<img src="{svg.as_uri()}" width="{pixels}" height="{pixels}"></body>',
        encoding="utf-8",
    )
    shot = scratch / f"{svg.stem}-{size}.png"
    subprocess.run(
        [
            str(EDGE),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--default-background-color=00000000",
            f"--user-data-dir={scratch / 'edge-profile'}",
            f"--window-size={pixels},{pixels}",
            f"--screenshot={shot}",
            page.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    # Edge can exit before its screenshot is on disk; wait for a whole picture.
    deadline = time.monotonic() + 30
    while True:
        try:
            with Image.open(shot) as picture:
                return picture.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        except OSError:
            if time.monotonic() > deadline:
                raise RuntimeError(f"Edge drew no picture of {svg.name} at {size} px") from None
            time.sleep(0.2)


def build_icon(destination: Path = ICON) -> Path:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        pictures = [render(ICON_DIR / name, size, Path(scratch)) for size, name in SOURCES.items()]
    largest, *rest = pictures
    # Pillow keeps an appended picture for its own size instead of shrinking the largest.
    largest.save(
        destination, format="ICO", sizes=[picture.size for picture in pictures], append_images=rest
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--destination", type=Path, default=ICON)
    arguments = parser.parse_args(argv)
    print(f"Wrote {build_icon(arguments.destination)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
