"""Pillow-backed raster codec for image editing workflows."""

from __future__ import annotations

from functools import reduce
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops

from bokucho.application.contracts import RasterImage
from bokucho.engines.rpgmv_assets import (
    ENCRYPTED_SUFFIXES,
    decrypt,
    encrypt,
    key_for,
    open_bytes,
)
from bokucho.gal import dump_gal

try:
    import livemaker.GalImagePlugin  # noqa: F401
except ImportError:  # pragma: no cover - optional GAL support
    pass


_FORMATS = {
    ".bmp": "BMP",
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
}
_PNG_MODES = {"1", "L", "LA", "P", "RGB", "RGBA"}
#: How far a browser canvas, which keeps colour premultiplied by alpha in
#: 8 bits, may move a premultiplied channel of a picture it only passed on.
_CANVAS_ROUNDING = 2


def _premultiplied(image: Image.Image) -> Image.Image:
    """``image``'s RGBa bytes relabelled RGBA, so ImageChops compares them as they are."""
    return Image.frombytes("RGBA", image.size, image.convert("RGBA").convert("RGBa").tobytes())


def _changed(first: Image.Image, second: Image.Image) -> Image.Image:
    """Where ``second`` is not ``first`` as a canvas hands it back, as an ``L`` mask."""
    *colour, alpha = ImageChops.difference(_premultiplied(first), _premultiplied(second)).split()
    widest = reduce(ImageChops.lighter, colour)
    opaque = second.convert("RGBA").getchannel("A").point(lambda value: 255 if value == 255 else 0)
    return reduce(
        ImageChops.lighter,
        (
            widest.point(lambda value: 255 if value > _CANVAS_ROUNDING else 0),
            ImageChops.multiply(widest.point(lambda value: 255 if value else 0), opaque),
            alpha.point(lambda value: 255 if value else 0),
        ),
    )


def _premultiply(colour: tuple[int, int, int, int]) -> tuple[int, int, int]:
    red, green, blue, alpha = colour
    return (round(red * alpha / 255), round(green * alpha / 255), round(blue * alpha / 255))


def _faint_entry(
    palette: tuple[tuple[int, int, int, int], ...], colour: tuple[int, int, int, int]
) -> int | None:
    """The palette entry a canvas could have rounded into the partly transparent ``colour``."""
    if colour[3] == 255:
        return None
    target = _premultiply(colour)
    for index, entry in enumerate(palette):
        if entry[3] == colour[3] and all(
            abs(mine - theirs) <= _CANVAS_ROUNDING
            for mine, theirs in zip(_premultiply(entry), target, strict=True)
        ):
            return index
    return None


def _save_options(image_format: str, reference: RasterImage | None) -> dict[str, object]:
    options: dict[str, object] = {}
    if image_format == "JPEG":
        options.update(quality=95, subsampling=0)
    if reference is not None and image_format in {"JPEG", "PNG"}:
        if reference.icc_profile is not None:
            options["icc_profile"] = reference.icc_profile
        if reference.exif is not None:
            options["exif"] = reference.exif
    return options


def _repaint(
    output: Image.Image,
    edited: Image.Image,
    changed: Image.Image,
    palette: tuple[tuple[int, int, int, int], ...],
) -> None:
    """Give each changed pixel of the indexed ``output`` the entry of its edited colour."""
    bounds = changed.getbbox()
    if bounds is None:
        return
    lookup: dict[tuple[int, int, int, int], int | None] = {}
    for index, color in enumerate(palette):
        lookup.setdefault(color, index)
    changed_pixels, edited_pixels, output_pixels = changed.load(), edited.load(), output.load()
    left, top, right, bottom = bounds
    for y in range(top, bottom):
        for x in range(left, right):
            if not changed_pixels[x, y]:
                continue
            after = edited_pixels[x, y]
            if after not in lookup:
                lookup[after] = _faint_entry(palette, after)
            if lookup[after] is None:
                raise ValueError("Edited color is not available in the source palette")
            output_pixels[x, y] = lookup[after]


class PillowImageCodec:
    def same_picture(self, first: bytes, second: bytes) -> bool:
        """Whether two pictures show the same pixels, up to a canvas's rounding."""
        with Image.open(BytesIO(first)) as one, Image.open(BytesIO(second)) as other:
            return one.size == other.size and _changed(one, other).getbbox() is None

    def load(self, path: Path, *, source: Path | None = None) -> RasterImage:
        if path.suffix.lower() in ENCRYPTED_SUFFIXES:
            with Image.open(BytesIO(open_bytes(path, source=source))) as image:
                return self._to_raster(image)
        with Image.open(path) as image:
            return self._to_raster(image)

    def decode(
        self,
        data: bytes,
        *,
        source: Path | None = None,
    ) -> RasterImage:
        if source is not None and source.suffix.lower() in ENCRYPTED_SUFFIXES:
            key = key_for(source)
            assert key is not None, f"no encryption key found for {source}"
            data = decrypt(data, key)
        with Image.open(BytesIO(data)) as image:
            return self._to_raster(image)

    def encode(
        self,
        image: RasterImage,
        suffix: str,
        mode: str,
        *,
        reference: RasterImage | None = None,
        source: Path | None = None,
    ) -> bytes:
        if suffix.lower() == ".gal":
            with Image.open(BytesIO(image.data)) as decoded:
                decoded.load()
                return dump_gal(decoded)
        if suffix.lower() in ENCRYPTED_SUFFIXES:
            key = key_for(source) if source is not None else None
            if key is None:
                raise ValueError(f"No encryption key found to save {suffix} image")
            return encrypt(self.encode(image, ".png", mode, reference=reference), key)
        image_format = _FORMATS.get(suffix.lower())
        if image_format is None:
            raise ValueError(f"Unsupported image output format: {suffix}")
        with Image.open(BytesIO(image.data)) as decoded:
            decoded.load()
            output_image = self._output_image(decoded, image_format, mode, reference)
            try:
                output = BytesIO()
                output_image.save(
                    output, format=image_format, **_save_options(image_format, reference)
                )
                return output.getvalue()
            finally:
                if output_image is not decoded:
                    output_image.close()

    def _output_image(
        self,
        decoded: Image.Image,
        image_format: str,
        mode: str,
        reference: RasterImage | None,
    ) -> Image.Image:
        if mode == "P":
            output_image = self._apply_palette_edits(decoded, reference)
        else:
            output_image = self._keep_untouched(decoded, reference)
            if output_image.mode != mode:
                output_image = output_image.convert(mode)
        if image_format == "JPEG" and output_image.mode not in {"CMYK", "L", "RGB"}:
            if output_image is not decoded:
                output_image.close()
            output_image = decoded.convert("RGB")
        return output_image

    @classmethod
    def _to_raster(cls, image: Image.Image) -> RasterImage:
        source_format = image.format
        source_mode = image.mode
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        icc_profile = image.info.get("icc_profile")
        exif = image.info.get("exif")
        palette = cls._palette_colors(image)
        image.load()
        normalized = image if image.mode in _PNG_MODES else image.convert("RGBA")
        output = BytesIO()
        normalized.save(output, format="PNG")
        return RasterImage(
            output.getvalue(),
            normalized.width,
            normalized.height,
            source_mode,
            source_format,
            has_alpha,
            icc_profile if isinstance(icc_profile, bytes) else None,
            exif if isinstance(exif, bytes) else None,
            palette,
        )

    @staticmethod
    def _palette_colors(image: Image.Image) -> tuple[tuple[int, int, int, int], ...] | None:
        if image.mode != "P":
            return None
        raw_palette = image.getpalette()
        if raw_palette is None:
            return None
        transparency = image.info.get("transparency")
        colors: list[tuple[int, int, int, int]] = []
        for index in range(len(raw_palette) // 3):
            offset = index * 3
            alpha = 255
            if isinstance(transparency, int) and index == transparency:
                alpha = 0
            elif isinstance(transparency, bytes) and index < len(transparency):
                alpha = transparency[index]
            colors.append((*raw_palette[offset : offset + 3], alpha))
        return tuple(colors)

    @staticmethod
    def _keep_untouched(edited: Image.Image, reference: RasterImage | None) -> Image.Image:
        """``edited`` with the faint pixels a canvas only passed on put back as they were."""
        if reference is None or not reference.has_alpha:
            return edited
        with Image.open(BytesIO(reference.data)) as original:
            if original.size != edited.size:
                return edited
            original = original.convert("RGBA")
        kept = edited.convert("RGBA")
        kept.paste(original, mask=ImageChops.invert(_changed(original, kept)))
        return kept

    @staticmethod
    def _apply_palette_edits(
        edited: Image.Image,
        reference: RasterImage | None,
    ) -> Image.Image:
        if reference is None or reference.palette is None:
            raise ValueError("Indexed image palette is unavailable")
        with Image.open(BytesIO(reference.data)) as indexed:
            indexed.load()
            if indexed.mode != "P":
                raise ValueError("Indexed image reference is not palette-based")
            original = indexed.convert("RGBA")
            edited_rgba = edited.convert("RGBA")
            output = indexed.copy()
        try:
            _repaint(output, edited_rgba, _changed(original, edited_rgba), reference.palette)
        except Exception:
            output.close()
            raise
        finally:
            original.close()
            edited_rgba.close()
        return output
