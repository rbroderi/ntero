"""Native texture encoding and legacy DDS compatibility tests."""

import binascii
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from ntero.alpha import AlphaMismatchError
from ntero.encoder import (
    GAME_DDS_FORMAT,
    GAME_LOSSY_DDS_FORMAT,
    TextureEncodeError,
    encode_from_png,
    encode_png_bytes,
    validate_game_dds,
)

DDS_BITS_PER_PIXEL = 32
EXPECTED_MIP_COUNT = 1
MIN_MIP_DIMENSION = 4
LEGACY_BGRA_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)


def _dds_payload(width: int, height: int, *, lossy: bool = False) -> bytes:
    payload = bytearray(128)
    payload[:4] = b"DDS "
    struct.pack_into("<I", payload, 4, 124)
    struct.pack_into(
        "<II8xI",
        payload,
        12,
        height,
        width,
        max(width, height).bit_length(),
    )
    if lossy:
        struct.pack_into("<II", payload, 76, 32, 4)
        payload[84:88] = b"DXT5"
    else:
        struct.pack_into("<II", payload, 76, 32, 0x41)
        struct.pack_into("<I", payload, 88, 32)
        struct.pack_into("<IIII", payload, 92, *LEGACY_BGRA_MASKS)
    return bytes(payload)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFF_FFFF
    return (
        struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)
    )


def _png(width: int, height: int, rows: bytes) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )


def _rgba_png(width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
    rows = b"".join(b"\0" + bytes(color) * width for _ in range(height))
    return _png(width, height, rows)


def _split_rgba_png(
    width: int,
    height: int,
    *,
    top: tuple[int, int, int, int],
    bottom: tuple[int, int, int, int],
) -> bytes:
    rows = b"".join(
        b"\0" + bytes(top if row < height // 2 else bottom) * width
        for row in range(height)
    )
    return _png(width, height, rows)


class EncoderTests(unittest.TestCase):
    """Verify lossless and lossy DDS output accepted by The Game."""

    def test_encodes_maximum_quality_game_dds(self) -> None:
        """Encode legacy BGRA without unusably small mip levels."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "edited.png"
            destination = root / "texture.dds"
            source.write_bytes(
                _split_rgba_png(
                    8,
                    4,
                    top=(10, 20, 30, 40),
                    bottom=(50, 60, 70, 80),
                ),
            )

            encoded = encode_from_png(source, destination, "texture.dds")
            payload = destination.read_bytes()

            assert encoded == payload
            assert GAME_DDS_FORMAT == "B8G8R8A8_UNORM"
            assert payload[:4] == b"DDS "
            assert payload[84:88] == b"\0\0\0\0"
            assert struct.unpack_from("<I", payload, 88)[0] == DDS_BITS_PER_PIXEL
            assert struct.unpack_from("<IIII", payload, 92) == LEGACY_BGRA_MASKS
            assert struct.unpack_from("<I", payload, 28)[0] == EXPECTED_MIP_COUNT
            assert struct.unpack_from("<I", payload, 20)[0] == 8 * 4
            assert payload[128:132] == bytes((30, 20, 10, 40))

    def test_encodes_without_writing_an_intermediate_file(self) -> None:
        """Return a valid DDS payload entirely in memory."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "edited.png"
            source.write_bytes(_rgba_png(4, 4, (10, 20, 30, 255)))

            payload = encode_png_bytes(source, "texture.dds", lossy=True)

            assert payload[:4] == b"DDS "
            assert payload[84:88] == b"DXT5"
            assert list(Path(temporary).iterdir()) == [source]

    def test_encodes_best_legacy_lossy_game_dds(self) -> None:
        """Encode legacy BC3/DXT5 without unusably small mip levels."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "edited.png"
            destination = root / "texture.dds"
            source.write_bytes(_rgba_png(8, 4, (10, 20, 30, 40)))

            encode_from_png(source, destination, "texture.dds", lossy=True)
            payload = destination.read_bytes()

            assert GAME_LOSSY_DDS_FORMAT == "BC3_UNORM"
            assert payload[:4] == b"DDS "
            assert payload[84:88] == b"DXT5"
            assert struct.unpack_from("<I", payload, 28)[0] == EXPECTED_MIP_COUNT
            validate_game_dds(payload, lossy=True)

    def test_default_mode_ignores_original_dxt_format(self) -> None:
        """Encode DXT sources as uncompressed BGRA in default mode."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "edited.png"
            destination = root / "texture.dds"
            source.write_bytes(_rgba_png(8, 4, (10, 20, 30, 255)))
            original = bytearray(_dds_payload(8, 4))
            struct.pack_into("<II", original, 76, 32, 4)
            original[84:88] = b"DXT1"

            encode_from_png(
                source,
                destination,
                "texture.dds",
                source_dds=bytes(original),
            )

            assert destination.read_bytes()[84:88] == b"\0\0\0\0"

    def test_logical_bmp_uses_requested_encoding_mode(self) -> None:
        """Write DDS payloads for logical BMP members in both modes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "texture.png"
            source.write_bytes(_rgba_png(8, 8, (10, 20, 30, 255)))

            lossless = encode_from_png(source, root / "lossless.bmp", "lossless.bmp")
            lossy = encode_from_png(
                source,
                root / "lossy.bmp",
                "lossy.bmp",
                lossy=True,
            )

            assert lossless[:4] == b"DDS "
            assert lossless[84:88] == b"\0\0\0\0"
            assert lossy[:4] == b"DDS "
            assert lossy[84:88] == b"DXT5"

    def test_rejects_dx10_header(self) -> None:
        """Reject DDS DX10 headers unsupported by the legacy client."""
        payload = bytearray(148)
        payload[:4] = b"DDS "
        struct.pack_into("<I", payload, 4, 124)
        payload[84:88] = b"DX10"

        with pytest.raises(TextureEncodeError, match="DX10"):
            validate_game_dds(bytes(payload))


@given(
    width=st.integers(min_value=1, max_value=8192),
    height=st.integers(min_value=1, max_value=8192),
    lossy=st.booleans(),
)
def test_accepts_complete_mip_chains(width: int, height: int, *, lossy: bool) -> None:
    """Accept exactly one complete mip chain for arbitrary positive dimensions."""
    payload = bytearray(_dds_payload(width, height, lossy=lossy))
    mip_count = 1
    while (
        width >> mip_count >= MIN_MIP_DIMENSION
        and height >> mip_count >= MIN_MIP_DIMENSION
    ):
        mip_count += 1
    struct.pack_into("<I", payload, 28, mip_count)
    validate_game_dds(bytes(payload), lossy=lossy)


@pytest.mark.parametrize(
    ("payload", "lossy", "message"),
    [
        (b"short", False, "complete DDS"),
        (b"BAD " + bytes(124), False, "complete DDS"),
        (b"DDS " + bytes(124), False, "legacy header size"),
        (_dds_payload(4, 4, lossy=False), True, "BC3/DXT5"),
        (_dds_payload(4, 4, lossy=True), False, "B8G8R8A8"),
    ],
)
def test_rejects_invalid_dds_formats(
    payload: bytes,
    *,
    lossy: bool,
    message: str,
) -> None:
    """Reject incomplete headers and formats requested in the wrong mode."""
    with pytest.raises(TextureEncodeError, match=message):
        validate_game_dds(payload, lossy=lossy)


@pytest.mark.parametrize(("width", "height"), [(0, 4), (4, 0)])
def test_rejects_invalid_dds_dimensions(width: int, height: int) -> None:
    """Reject zero dimensions even when the encoded mip count matches."""
    with pytest.raises(TextureEncodeError, match="mip levels"):
        validate_game_dds(_dds_payload(width, height))


def test_rejects_incomplete_mip_chain() -> None:
    """Reject a valid pixel format with the wrong number of mip levels."""
    payload = bytearray(_dds_payload(16, 8))
    struct.pack_into("<I", payload, 28, 1)

    with pytest.raises(TextureEncodeError, match="mip levels"):
        validate_game_dds(bytes(payload))


@pytest.mark.parametrize("suffix", [".bmp", ".tga"])
def test_native_non_dds_names_receive_bgra_dds(suffix: str) -> None:
    """Write compatible DDS payloads for logical BMP and TGA member names."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.png"
        destination = root / f"texture{suffix}"
        source.write_bytes(_rgba_png(4, 4, (10, 20, 30, 255)))

        encode_from_png(source, destination, f"texture{suffix}")

        with Image.open(destination) as encoded:
            assert encoded.convert("RGBA").getpixel((0, 0)) == (10, 20, 30, 255)
        assert destination.read_bytes()[:4] == b"DDS "


def test_native_encode_failure_is_translated() -> None:
    """Expose native failures through the texture encoding error contract."""
    with (
        patch("ntero.encoder._encode_with_native", side_effect=RuntimeError("failed")),
        pytest.raises(TextureEncodeError, match=r"Native encoding failed.*failed"),
    ):
        encode_from_png(Path("source.png"), Path("texture.dds"), "texture.dds")


def test_native_alpha_validation_rejects_before_writing_output() -> None:
    """Classify alpha during native decode and reject incompatible edits."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "edited.png"
        destination = root / "texture.dds"
        Image.new("RGB", (2, 1), (10, 20, 30)).save(source, format="PNG")

        with pytest.raises(AlphaMismatchError, match="graded to none"):
            encode_from_png(
                source,
                destination,
                "texture.dds",
                expected_alpha="graded",
            )

        assert not destination.exists()


def test_native_alpha_validation_accepts_compatible_edit() -> None:
    """Allow graded alpha where the source required binary capability."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "edited.png"
        destination = root / "texture.dds"
        source.write_bytes(_rgba_png(4, 4, (10, 20, 30, 128)))

        encode_from_png(
            source,
            destination,
            "texture.dds",
            expected_alpha="binary",
        )

        assert destination.is_file()


def test_native_alpha_validation_recognizes_palette_transparency() -> None:
    """Preserve transparent classification for indexed PNG metadata."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "indexed.png"
        destination = root / "texture.dds"
        image = Image.new("P", (1, 1), 0)
        image.putpalette([10, 20, 30] + [0, 0, 0] * 255)
        image.save(source, format="PNG", transparency=0)

        encode_from_png(
            source,
            destination,
            "texture.dds",
            expected_alpha="transparent",
        )

        assert destination.is_file()


def test_rejects_unsupported_encode_extension() -> None:
    """Reject packed texture types without an encoding policy."""
    with pytest.raises(TextureEncodeError, match="Unsupported packed texture"):
        encode_from_png(Path("a.png"), Path("a.gif"), "a.gif")
