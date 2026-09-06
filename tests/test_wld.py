"""WLD texture metadata tests."""

import struct

import pytest

from ntero.wld import WLD_MAGIC
from ntero.wld import WLD_STRING_KEY
from ntero.wld import WldError
from ntero.wld import masked_palette_indices


def _encoded(value: bytes) -> bytes:
    return bytes(
        byte ^ WLD_STRING_KEY[index % len(WLD_STRING_KEY)]
        for index, byte in enumerate(value)
    )


def _fragment(kind: int, body: bytes) -> bytes:
    return struct.pack("<II", len(body), kind) + body


def _wld(filename: str, material_type: int) -> bytes:
    encoded_name = _encoded(filename.encode() + b"\0")
    fragments = [
        _fragment(0x03, struct.pack("<iIH", 0, 1, len(encoded_name)) + encoded_name),
        _fragment(0x04, struct.pack("<iII", 0, 0, 1) + struct.pack("<I", 1)),
        _fragment(0x05, struct.pack("<iII", 0, 2, 0)),
        _fragment(
            0x30,
            struct.pack("<iII4BffI", 0, 0, material_type, 0, 0, 0, 0, 0, 0, 3),
        ),
    ]
    header = struct.pack(
        "<7I",
        WLD_MAGIC,
        0x0001_5500,
        len(fragments),
        0,
        0,
        0,
        0,
    )
    return header + b"".join(fragments)


def test_masked_palette_indices_follows_material_references() -> None:
    """Recognize masked material metadata and the standard palette index."""
    assert masked_palette_indices(_wld("curlytube.bmp", 0x8000_0013)) == {
        "curlytube.bmp": 0,
    }
    assert masked_palette_indices(_wld("ordinary.bmp", 0x01)) == {}


def test_masked_palette_indices_applies_known_exception() -> None:
    """Use the established nonzero palette indices for exceptional textures."""
    assert masked_palette_indices(_wld("BEARRUG.BMP", 0x19)) == {
        "bearrug.bmp": 47,
    }


def test_masked_palette_indices_rejects_truncated_wld() -> None:
    """Reject incomplete WLD input rather than inferring metadata."""
    with pytest.raises(WldError, match="truncated"):
        masked_palette_indices(b"short")
