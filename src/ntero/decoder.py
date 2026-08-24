"""Decode packed textures into editable PNG files."""

from pathlib import Path

from PIL import Image

PNG_COMPRESSION_LEVEL = 3


class TextureDecodeError(RuntimeError):
    """Raised when Pillow cannot decode a texture."""


def _decode_with_pillow(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.load()
        image.save(
            destination,
            format="PNG",
            compress_level=PNG_COMPRESSION_LEVEL,
        )


def decode_to_png(source: Path, destination: Path) -> None:
    """Decode a texture to PNG with Pillow."""
    try:
        _decode_with_pillow(source, destination)
    except OSError as pillow_error:
        destination.unlink(missing_ok=True)
        msg = f"Could not decode {source.name} with Pillow ({pillow_error})"
        raise TextureDecodeError(msg) from pillow_error
