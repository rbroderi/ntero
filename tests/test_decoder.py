"""Pillow-first texture decoding tests."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from ntero.decoder import (
    PNG_COMPRESSION_LEVEL,
    TextureDecodeError,
    _decode_with_pillow,
    decode_to_png,
)


def test_png_output_uses_fast_compression_level() -> None:
    """Write editable PNGs with the explicit fast compression level."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.bmp"
        destination = root / "editable.png"
        Image.new("RGB", (1, 1)).save(source, format="BMP")

        with patch("PIL.Image.Image.save", autospec=True) as save:
            _decode_with_pillow(source, destination)

        save.assert_called_once()
        assert save.call_args.args[1] == destination
        assert save.call_args.kwargs == {
            "format": "PNG",
            "compress_level": PNG_COMPRESSION_LEVEL,
        }


@pytest.mark.parametrize(
    ("extension", "image_format", "mode"),
    [(".bmp", "BMP", "RGB"), (".tga", "TGA", "RGBA"), (".dds", "DDS", "RGBA")],
)
def test_pillow_decodes_supported_textures(
    extension: str,
    image_format: str,
    mode: str,
) -> None:
    """Preserve dimensions and RGBA pixels when Pillow supports the source."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / f"source{extension}"
        destination = root / "editable.png"
        expected = [(10, 20, 30, 255), (40, 50, 60, 128)]
        source_pixels = (
            expected if mode == "RGBA" else [pixel[:3] for pixel in expected]
        )
        raw_pixels = bytes(channel for pixel in source_pixels for channel in pixel)
        image = Image.frombytes(mode, (2, 1), raw_pixels)
        image.save(source, format=image_format)

        decode_to_png(source, destination)

        with Image.open(destination) as decoded:
            assert decoded.size == (2, 1)
            assert list(decoded.convert("RGBA").get_flattened_data()) == [
                expected[0],
                expected[1] if mode == "RGBA" else (*expected[1][:3], 255),
            ]


def test_decode_failure_removes_partial_output_and_reports_pillow_error() -> None:
    """Remove partial output and report Pillow decode failures."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "broken.tga"
        destination = root / "editable.png"
        source.write_bytes(b"broken")

        def fail_pillow(_source: Path, output: Path) -> None:
            output.write_bytes(b"partial")
            message = "Pillow failure"
            raise OSError(message)

        with (
            patch("ntero.decoder._decode_with_pillow", side_effect=fail_pillow),
            pytest.raises(
                TextureDecodeError,
                match="Pillow failure",
            ),
        ):
            decode_to_png(source, destination)

        assert not destination.exists()
