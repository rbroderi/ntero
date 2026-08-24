"""PFS archive format and path-safety tests."""

import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck
from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from ntero.cli import _safe_member_path
from ntero.cli import _safe_pack_root
from ntero.pfs import FILENAME_DIRECTORY_CRC
from ntero.pfs import PFS_COMPRESSION_LEVEL
from ntero.pfs import PFS_MAGIC
from ntero.pfs import PFS_VERSION
from ntero.pfs import UINT32_MAX
from ntero.pfs import PfsArchive
from ntero.pfs import PfsEntry
from ntero.pfs import PfsError
from ntero.pfs import _compress_payload
from ntero.pfs import _StoredEntry
from ntero.pfs import filename_crc

TINY_TGA_CRC = 0x1DDE_80E1
EXPECTED_COMPRESSED_CHUNKS = 2


def test_payload_compression_uses_fast_level() -> None:
    """Compress each archive chunk with the explicit fast zlib level."""
    with patch("ntero.pfs.zlib.compress", wraps=zlib.compress) as compress:
        _compress_payload(b"x" * 9000)

    assert compress.call_count == EXPECTED_COMPRESSED_CHUNKS
    assert all(
        call.kwargs == {"level": PFS_COMPRESSION_LEVEL}
        for call in compress.call_args_list
    )


def _create_archive(items: dict[str, bytes]) -> bytes:
    names = sorted(items, key=str.casefold)
    filename_payload = bytearray(struct.pack("<I", len(names)))
    for name in names:
        encoded = name.encode("latin-1") + b"\0"
        filename_payload.extend(struct.pack("<I", len(encoded)))
        filename_payload.extend(encoded)

    payloads = [(filename_crc(name), items[name]) for name in names]
    payloads.append((FILENAME_DIRECTORY_CRC, bytes(filename_payload)))
    output = bytearray(struct.pack("<I4sI", 0, PFS_MAGIC, PFS_VERSION))
    records: list[tuple[int, int, int]] = []
    for crc, payload in payloads:
        offset = len(output)
        compressed = zlib.compress(payload)
        output.extend(struct.pack("<II", len(compressed), len(payload)))
        output.extend(compressed)
        records.append((crc, offset, len(payload)))
    directory_offset = len(output)
    output.extend(struct.pack("<I", len(records)))
    for record in records:
        output.extend(struct.pack("<III", *record))
    struct.pack_into("<I", output, 0, directory_offset)
    return bytes(output)


class PfsTests(unittest.TestCase):
    """Verify PFS CRC, rebuilding, and safe path behavior."""

    def test_filename_crc_is_ascii_case_insensitive(self) -> None:
        """Compute stable case-insensitive legacy filename CRC values."""
        assert filename_crc("Texture.DDS") == filename_crc("texture.dds")
        assert filename_crc("tiny.tga") == TINY_TGA_CRC

    def test_rebuild_replaces_one_entry_and_preserves_another(self) -> None:
        """Replace requested members while preserving all other payloads."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.s3d"
            destination = root / "rebuilt.s3d"
            source.write_bytes(
                _create_archive({"first.bmp": b"first", "data.wld": b"world"}),
            )

            archive = PfsArchive(source)
            archive.rebuild(destination, {"first.bmp": b"replacement"})
            rebuilt = PfsArchive(destination)

            assert rebuilt.read("first.bmp") == b"replacement"
            assert rebuilt.read("data.wld") == b"world"

    def test_rebuild_rejects_unknown_entry(self) -> None:
        """Reject replacements that do not exist in the source archive."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.s3d"
            source.write_bytes(_create_archive({"first.bmp": b"first"}))

            with pytest.raises(PfsError, match="do not exist"):
                PfsArchive(source).rebuild(root / "out.s3d", {"missing.bmp": b"x"})

    def test_maps_one_filename_with_a_stale_crc(self) -> None:
        """Recover an unambiguous member renamed without updating its CRC."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stale.s3d"
            payload = bytearray(_create_archive({"renamed (New).bmp": b"texture"}))
            directory_offset = struct.unpack_from("<I", payload)[0]
            struct.pack_into("<I", payload, directory_offset + 4, 0x1234_5678)
            source.write_bytes(payload)

            archive = PfsArchive(source)

            assert archive.read("renamed (New).bmp") == b"texture"

    def test_maps_multiple_stale_crcs_by_filename_order(self) -> None:
        """Recover renamed members whose records retain stale CRC values."""
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "stale.s3d"
            items = {
                "first (New).png": b"first",
                "second (New).png": b"second",
                "third (New).png": b"third",
            }
            payload = bytearray(_create_archive(items))
            directory_offset = struct.unpack_from("<I", payload)[0]
            for index, crc in enumerate((0x1234_5678, 0x2345_6789, 0x3456_789A)):
                struct.pack_into("<I", payload, directory_offset + 4 + index * 12, crc)
            source.write_bytes(payload)

            archive = PfsArchive(source)

            for name, expected in items.items():
                assert archive.read(name) == expected

    def test_paths_reject_traversal(self) -> None:
        """Reject archive and pack paths that escape their assigned roots."""
        with pytest.raises(ValueError, match="Unsafe archive member path"):
            _safe_member_path("../outside.dds")
        with pytest.raises(ValueError, match="one directory name"):
            _safe_pack_root(Path("library"), "../outside")


@settings(suppress_health_check=[HealthCheck.data_too_large])
@given(
    payload=st.one_of(
        st.binary(max_size=256),
        st.binary(min_size=8193, max_size=20_000),
    ),
    replacement=st.binary(max_size=1024),
)
def test_archive_round_trips_arbitrary_payloads(
    payload: bytes,
    replacement: bytes,
) -> None:
    """Preserve arbitrary bytes across parsing and multi-chunk rebuilding."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.s3d"
        rebuilt_path = root / "rebuilt.s3d"
        source.write_bytes(_create_archive({"Texture.DDS": payload}))

        archive = PfsArchive(source)
        assert archive.read(archive.entries[0]) == payload
        archive.rebuild(rebuilt_path, {"texture.dds": replacement})

        assert PfsArchive(rebuilt_path).read("TEXTURE.DDS") == replacement


@given(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-",
        max_size=40,
    ),
)
def test_filename_crc_is_case_insensitive_for_ascii(name: str) -> None:
    """Apply ASCII case folding consistently for arbitrary archive names."""
    assert filename_crc(name) == filename_crc(name.swapcase())


def _unparsed_archive(data: bytes) -> PfsArchive:
    archive = object.__new__(PfsArchive)
    archive._data = data
    return archive


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"short", "too short"),
        (struct.pack("<I4sII", 12, b"BAD!", PFS_VERSION, 1), "PFS 2"),
        (struct.pack("<I4sII", 12, PFS_MAGIC, 1, 1), "PFS 2"),
        (struct.pack("<I4sII", 99, PFS_MAGIC, PFS_VERSION, 1), "offset"),
        (struct.pack("<I4sII", 12, PFS_MAGIC, PFS_VERSION, 0), "count"),
        (
            struct.pack("<I4sII", 12, PFS_MAGIC, PFS_VERSION, 250_001),
            "count",
        ),
        (struct.pack("<I4sII", 12, PFS_MAGIC, PFS_VERSION, 1), "truncated"),
    ],
)
def test_rejects_invalid_directories(data: bytes, message: str) -> None:
    """Reject malformed archive headers and directory tables."""
    with pytest.raises(PfsError, match=message):
        _unparsed_archive(data)._read_directory()


def test_filename_directory_uses_legacy_fallback() -> None:
    """Accept the legacy all-ones CRC for the filename directory."""
    fallback = _StoredEntry(0, UINT32_MAX, 12, 1, 1)
    assert PfsArchive._find_filename_entry([fallback]) is fallback


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [
            _StoredEntry(0, FILENAME_DIRECTORY_CRC, 12, 1, 1),
            _StoredEntry(1, FILENAME_DIRECTORY_CRC, 13, 1, 1),
        ],
    ],
)
def test_requires_one_filename_directory(entries: list[_StoredEntry]) -> None:
    """Reject missing and ambiguous filename directory records."""
    with pytest.raises(PfsError, match="one filename directory"):
        PfsArchive._find_filename_entry(entries)


def test_rejects_unmapped_directory_record() -> None:
    """Reject logical records that have no matching filename."""
    filename_entry = _StoredEntry(0, FILENAME_DIRECTORY_CRC, 12, 1, 1)
    orphan = _StoredEntry(1, filename_crc("orphan.dds"), 13, 1, 1)

    with pytest.raises(PfsError, match="could not be mapped"):
        PfsArchive._map_entries([], [filename_entry, orphan], filename_entry)


def test_rejects_filename_without_a_data_record() -> None:
    """Reject filename directories containing more names than data records."""
    filename_entry = _StoredEntry(0, FILENAME_DIRECTORY_CRC, 12, 1, 1)

    with pytest.raises(PfsError, match="could not be mapped"):
        PfsArchive._map_entries(
            ["missing.dds", "present.dds"],
            [
                filename_entry,
                _StoredEntry(1, filename_crc("present.dds"), 13, 1, 1),
            ],
            filename_entry,
        )


def test_ignores_trailing_filename_without_a_data_record() -> None:
    """Accept legacy archives whose filename table ends with a ghost entry."""
    filename_entry = _StoredEntry(1, FILENAME_DIRECTORY_CRC, 13, 1, 1)
    present = _StoredEntry(0, filename_crc("present.dds"), 12, 1, 1)

    assert PfsArchive._map_entries(
        ["present.dds", "missing.wld"],
        [present, filename_entry],
        filename_entry,
    ) == [PfsEntry("present.dds", present)]


@pytest.mark.parametrize(
    ("data", "offset", "size", "directory_offset", "message"),
    [
        (bytes(32), 1, 1, 24, "outside"),
        (bytes(16), 12, 1, 16, "header is truncated"),
        (struct.pack("<II", 0, 1) + bytes(16), 0, 1, 16, "outside"),
        (bytes(12) + struct.pack("<II", 0, 1), 12, 1, 20, "lengths"),
        (bytes(12) + struct.pack("<II", 1, 0), 12, 1, 20, "lengths"),
        (bytes(12) + struct.pack("<II", 1, 2) + b"x", 12, 1, 21, "lengths"),
        (bytes(12) + struct.pack("<II", 2, 1) + b"x", 12, 1, 21, "exceeds"),
    ],
)
def test_rejects_invalid_chunks(
    data: bytes,
    offset: int,
    size: int,
    directory_offset: int,
    message: str,
) -> None:
    """Reject chunks outside the data area or with inconsistent lengths."""
    with pytest.raises(PfsError, match=message):
        _unparsed_archive(data)._validate_chunks(offset, size, directory_offset)


def test_rejects_decompressed_chunk_length_mismatch() -> None:
    """Verify each zlib chunk against its declared uncompressed size."""
    compressed = zlib.compress(b"data")
    data = bytes(12) + struct.pack("<II", len(compressed), 3) + compressed
    entry = _StoredEntry(0, 0, 12, 3, len(data) - 12)

    with pytest.raises(PfsError, match="zlib chunk length"):
        _unparsed_archive(data)._decompress(entry)


def test_rejects_decompressed_entry_length_mismatch() -> None:
    """Verify the final payload against the directory record size."""
    compressed = zlib.compress(b"ab")
    data = bytes(12) + struct.pack("<II", len(compressed), 2) + compressed
    entry = _StoredEntry(0, 0, 12, 1, len(data) - 12)

    with pytest.raises(PfsError, match="entry length"):
        _unparsed_archive(data)._decompress(entry)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "truncated"),
        (struct.pack("<I", 1), "truncated"),
        (struct.pack("<II", 1, 0), "null terminated"),
        (struct.pack("<II", 1, 3) + b"a\0b", "null terminated"),
        (struct.pack("<I", 0) + b"extra", "trailing bytes"),
    ],
)
def test_rejects_invalid_filename_directories(payload: bytes, message: str) -> None:
    """Reject truncated, unterminated, and overlong filename payloads."""
    with pytest.raises(PfsError, match=message):
        PfsArchive._parse_names(payload)


if __name__ == "__main__":
    unittest.main()
