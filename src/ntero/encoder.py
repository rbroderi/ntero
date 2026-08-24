"""Encode and validate texture output for The Game."""

import struct
from pathlib import Path

from ntero.alpha import AlphaMismatchError, AlphaMode

GAME_DDS_FORMAT = "B8G8R8A8_UNORM"
GAME_LOSSY_DDS_FORMAT = "BC3_UNORM"
ENCODING_POLICY_VERSION = "native-uniform-dds-mip4-v5"
_LEGACY_DDS_FORMATS = {
    b"DXT1": "BC1_UNORM",
    b"DXT3": "BC2_UNORM",
    b"DXT5": "BC3_UNORM",
}
_LEGACY_FOUR_CC = {value: key for key, value in _LEGACY_DDS_FORMATS.items()}
_DDS_PIXEL_FORMAT_FLAGS = 0x41
_DDS_COLOR_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
DDS_HEADER_BYTES = 128
DDS_LEGACY_HEADER_SIZE = 124
DDS_PIXEL_FORMAT_OFFSET = 76
DDS_FOUR_CC_OFFSET = 84
DDS_BITS_PER_PIXEL_OFFSET = 88
DDS_COLOR_MASKS_OFFSET = 92
DDS_PIXEL_FORMAT_SIZE = 32
DDS_FOUR_CC_FLAG = 0x4
_MIN_MIP_DIMENSION = 4


class TextureEncodeError(RuntimeError):
    """Raised when a texture cannot be encoded for The Game."""


def encoding_key(*, lossy: bool) -> str:
    """Identify settings that affect encoded output bytes."""
    mode = "bc3" if lossy else "bgra"
    return f"{ENCODING_POLICY_VERSION}:{mode}"


def _kept_mip_count(width: int, height: int) -> int:
    count = 1
    while (
        width >> count >= _MIN_MIP_DIMENSION and height >> count >= _MIN_MIP_DIMENSION
    ):
        count += 1
    return count


def _encode_with_native(
    source: Path,
    format_name: str,
    expected_alpha: AlphaMode | None,
) -> bytes:
    from ntero import _native  # noqa: PLC0415

    return _native.encode_png_bytes(
        str(source.resolve()),
        format_name,
        expected_alpha,
    )


def validate_game_dds(
    payload: bytes,
    *,
    lossy: bool = False,
    format_name: str = GAME_DDS_FORMAT,
) -> None:
    """Validate a full-mip legacy DDS payload for The Game."""
    if len(payload) < DDS_HEADER_BYTES or payload[:4] != b"DDS ":
        msg = "Encoder did not produce a complete DDS file"
        raise TextureEncodeError(msg)
    if struct.unpack_from("<I", payload, 4)[0] != DDS_LEGACY_HEADER_SIZE:
        msg = "DDS output has an invalid legacy header size"
        raise TextureEncodeError(msg)

    pixel_format_size, pixel_format_flags = struct.unpack_from(
        "<II",
        payload,
        DDS_PIXEL_FORMAT_OFFSET,
    )
    four_cc = payload[DDS_FOUR_CC_OFFSET:DDS_BITS_PER_PIXEL_OFFSET]
    bits_per_pixel = struct.unpack_from("<I", payload, DDS_BITS_PER_PIXEL_OFFSET)[0]
    color_masks = struct.unpack_from("<IIII", payload, DDS_COLOR_MASKS_OFFSET)
    if four_cc == b"DX10":
        msg = "DDS DX10 headers are not supported by The Game"
        raise TextureEncodeError(msg)
    expected_format = GAME_LOSSY_DDS_FORMAT if lossy else format_name
    expected_four_cc = _LEGACY_FOUR_CC.get(expected_format)
    if expected_four_cc is not None:
        if (
            pixel_format_size != DDS_PIXEL_FORMAT_SIZE
            or pixel_format_flags != DDS_FOUR_CC_FLAG
            or four_cc != expected_four_cc
        ):
            format_label = expected_format.removesuffix("_UNORM")
            msg = f"DDS output is not legacy {format_label}/{expected_four_cc.decode()}"
            raise TextureEncodeError(msg)
    elif (
        pixel_format_size != DDS_PIXEL_FORMAT_SIZE
        or pixel_format_flags != _DDS_PIXEL_FORMAT_FLAGS
        or four_cc != b"\0\0\0\0"
        or bits_per_pixel != DDS_PIXEL_FORMAT_SIZE
        or color_masks != _DDS_COLOR_MASKS
    ):
        msg = "DDS output is not legacy uncompressed A8R8G8B8/B8G8R8A8"
        raise TextureEncodeError(msg)

    height, width, mip_count = struct.unpack_from("<II8xI", payload, 12)
    expected_mip_count = _kept_mip_count(width, height)
    if width == 0 or height == 0 or mip_count != expected_mip_count:
        msg = (
            f"DDS output has {mip_count} mip levels; {expected_mip_count} were required"
        )
        raise TextureEncodeError(msg)


def encode_png_bytes(
    source: Path,
    original_name: str,
    *,
    lossy: bool = False,
    source_dds: bytes | None = None,
    expected_alpha: AlphaMode | None = None,
) -> bytes:
    """Encode an edited PNG to an in-memory packed texture payload."""
    del source_dds
    extension = Path(original_name).suffix.lower()
    if extension not in {".dds", ".bmp", ".tga"}:
        msg = f"Unsupported packed texture extension: {extension}"
        raise TextureEncodeError(msg)
    dds_format = GAME_LOSSY_DDS_FORMAT if lossy else GAME_DDS_FORMAT
    try:
        payload = _encode_with_native(source, dds_format, expected_alpha)
    except ValueError as error:
        raise AlphaMismatchError(str(error)) from error
    except RuntimeError as error:
        msg = f"Native encoding failed for {source.name}: {error}"
        raise TextureEncodeError(msg) from error
    validate_game_dds(
        payload,
        lossy=lossy,
        format_name=dds_format,
    )
    return payload


def encode_from_png(  # noqa: PLR0913
    source: Path,
    destination: Path,
    original_name: str,
    *,
    lossy: bool = False,
    source_dds: bytes | None = None,
    expected_alpha: AlphaMode | None = None,
) -> bytes:
    """Encode an edited PNG and write its packed texture payload."""
    payload = encode_png_bytes(
        source,
        original_name,
        lossy=lossy,
        source_dds=source_dds,
        expected_alpha=expected_alpha,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return payload
