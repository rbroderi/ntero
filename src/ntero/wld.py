"""Read texture transparency metadata from EverQuest WLD files."""

import struct
from pathlib import PurePosixPath

WLDPath = PurePosixPath

WLD_MAGIC = 0x5450_3D02
WLD_HEADER_SIZE = 28
WLD_FRAGMENT_HEADER_SIZE = 8
WLD_STRING_KEY = bytes((0x95, 0x3A, 0xC5, 0x2A, 0x95, 0x7A, 0x95, 0x6A))
MATERIAL_TRANSPARENT_MASKED = 0x13
MATERIAL_DIFFUSE5 = 0x19
MATERIAL_TYPE_MASK = 0x7FFF_FFFF
DEFAULT_TRANSPARENT_PALETTE_INDEX = 0
BITMAP_NAME_FRAGMENT = 0x03
BITMAP_INFO_FRAGMENT = 0x04
BITMAP_INFO_REFERENCE_FRAGMENT = 0x05
MATERIAL_FRAGMENT = 0x30
BITMAP_NAME_HEADER_SIZE = 10
BITMAP_INFO_HEADER_SIZE = 12
BITMAP_INFO_REFERENCE_SIZE = 8
MATERIAL_SIZE = 28
BITMAP_INFO_ANIMATED_FLAG = 0x08
TRANSPARENT_PALETTE_INDEX_EXCEPTIONS = {
    "clhe0004.bmp": 255,
    "kahe0001.bmp": 255,
    "furpile1.bmp": 250,
    "bearrug.bmp": 47,
}


class WldError(ValueError):
    """Raised when a WLD file is malformed or unsupported."""


def _decode_string(value: bytes) -> str:
    return bytes(
        byte ^ WLD_STRING_KEY[index % len(WLD_STRING_KEY)]
        for index, byte in enumerate(value)
    ).decode("utf-8")


def _read_fragments(data: bytes) -> list[tuple[int, bytes]]:
    if len(data) < WLD_HEADER_SIZE:
        msg = "WLD header is truncated"
        raise WldError(msg)
    magic, _, count, _, _, string_hash_size, _ = struct.unpack_from("<7I", data)
    if magic != WLD_MAGIC:
        msg = "WLD magic is invalid"
        raise WldError(msg)
    position = WLD_HEADER_SIZE + string_hash_size
    fragments: list[tuple[int, bytes]] = []
    for _ in range(count):
        if position + WLD_FRAGMENT_HEADER_SIZE > len(data):
            msg = "WLD fragment header is truncated"
            raise WldError(msg)
        size, kind = struct.unpack_from("<II", data, position)
        position += WLD_FRAGMENT_HEADER_SIZE
        end = position + size
        if end > len(data):
            msg = "WLD fragment is truncated"
            raise WldError(msg)
        fragments.append((kind, data[position:end]))
        position = end
    return fragments


def _read_bitmap_name(body: bytes) -> str | None:
    if len(body) < BITMAP_NAME_HEADER_SIZE:
        return None
    name_size = struct.unpack_from("<H", body, 8)[0]
    end = BITMAP_NAME_HEADER_SIZE + name_size
    if end > len(body):
        return None
    filename = _decode_string(body[BITMAP_NAME_HEADER_SIZE:end]).rstrip("\0")
    return WLDPath(filename).name.casefold()


def _read_bitmap_info(body: bytes) -> list[int] | None:
    if len(body) < BITMAP_INFO_HEADER_SIZE:
        return None
    flags, bitmap_count = struct.unpack_from("<II", body, 4)
    offset = BITMAP_INFO_HEADER_SIZE
    if flags & BITMAP_INFO_ANIMATED_FLAG:
        offset += 4
    end = offset + bitmap_count * 4
    if end > len(body):
        return None
    return list(struct.unpack_from(f"<{bitmap_count}I", body, offset))


def _texture_references(
    fragments: list[tuple[int, bytes]],
) -> tuple[dict[int, str], dict[int, list[int]], dict[int, int]]:
    bitmap_names: dict[int, str] = {}
    bitmap_infos: dict[int, list[int]] = {}
    bitmap_info_references: dict[int, int] = {}
    for index, (kind, body) in enumerate(fragments, start=1):
        if kind == BITMAP_NAME_FRAGMENT:
            if (filename := _read_bitmap_name(body)) is not None:
                bitmap_names[index] = filename
        elif kind == BITMAP_INFO_FRAGMENT:
            if (references := _read_bitmap_info(body)) is not None:
                bitmap_infos[index] = references
        elif (
            kind == BITMAP_INFO_REFERENCE_FRAGMENT
            and len(body) >= BITMAP_INFO_REFERENCE_SIZE
        ):
            bitmap_info_references[index] = struct.unpack_from("<I", body, 4)[0]
    return bitmap_names, bitmap_infos, bitmap_info_references


def masked_palette_indices(data: bytes) -> dict[str, int]:
    """Map WLD masked bitmap names to their transparent palette indices."""
    fragments = _read_fragments(data)
    bitmap_names, bitmap_infos, bitmap_info_references = _texture_references(
        fragments,
    )

    masked: dict[str, int] = {}
    for kind, body in fragments:
        if kind != MATERIAL_FRAGMENT or len(body) < MATERIAL_SIZE:
            continue
        parameters = struct.unpack_from("<I", body, 8)[0] & MATERIAL_TYPE_MASK
        if parameters not in {MATERIAL_TRANSPARENT_MASKED, MATERIAL_DIFFUSE5}:
            continue
        reference = struct.unpack_from("<I", body, 24)[0]
        bitmap_info = bitmap_info_references.get(reference)
        for bitmap_name in bitmap_infos.get(bitmap_info, []):
            filename = bitmap_names.get(bitmap_name)
            if filename is not None:
                masked[filename] = TRANSPARENT_PALETTE_INDEX_EXCEPTIONS.get(
                    filename,
                    DEFAULT_TRANSPARENT_PALETTE_INDEX,
                )
    return masked
