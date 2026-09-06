"""Decode packed textures into editable PNG files."""

from pathlib import Path

from PIL import Image

PNG_COMPRESSION_LEVEL = 3
_MAGENTA_COLOR_KEY = (255, 0, 255)
_PALETTE_CHANNELS = 3
_PALETTE_SIZE = 256


class TextureDecodeError(RuntimeError):
    """Raised when Pillow cannot decode a texture."""


def _preserve_bmp_color_key(image: Image.Image) -> None:
    if image.format != "BMP" or image.mode != "P":
        return
    palette = image.getpalette()
    if palette is None:
        return
    alpha = bytearray([255] * _PALETTE_SIZE)
    found = False
    for index, offset in enumerate(range(0, len(palette), _PALETTE_CHANNELS)):
        if tuple(palette[offset : offset + _PALETTE_CHANNELS]) == _MAGENTA_COLOR_KEY:
            alpha[index] = 0
            found = True
    if found:
        image.info["transparency"] = bytes(alpha)


def _decode_with_pillow(
    source: Path,
    destination: Path,
    *,
    transparent_palette_index: int | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.load()
        if (
            image.format == "BMP"
            and image.mode == "P"
            and transparent_palette_index is not None
        ):
            alpha = bytearray([255] * _PALETTE_SIZE)
            alpha[transparent_palette_index] = 0
            image.info["transparency"] = bytes(alpha)
        else:
            _preserve_bmp_color_key(image)
        image.save(
            destination,
            format="PNG",
            compress_level=PNG_COMPRESSION_LEVEL,
        )


def decode_to_png(
    source: Path,
    destination: Path,
    *,
    transparent_palette_index: int | None = None,
) -> None:
    """Decode a texture to PNG with Pillow."""
    try:
        if transparent_palette_index is None:
            _decode_with_pillow(source, destination)
        else:
            _decode_with_pillow(
                source,
                destination,
                transparent_palette_index=transparent_palette_index,
            )
    except OSError as pillow_error:
        destination.unlink(missing_ok=True)
        msg = f"Could not decode {source.name} with Pillow ({pillow_error})"
        raise TextureDecodeError(msg) from pillow_error
