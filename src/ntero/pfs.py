"""Read, validate, and rebuild The Game's PFS version 2 archives."""

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

PFS_MAGIC = b"PFS "
PFS_VERSION = 0x0002_0000
FILENAME_DIRECTORY_CRC = 0x6158_0AC9
HEADER_SIZE = 12
CHUNK_SIZE = 8 * 1024
PFS_COMPRESSION_LEVEL = 1
DIRECTORY_RECORD_SIZE = 12
UINT32_SIZE = 4
MAX_ENTRY_COUNT = 250_000
UINT32_MAX = 0xFFFF_FFFF
CRC_HIGH_BIT = 0x8000_0000
CRC_POLYNOMIAL = 0x04C1_1DB7
CRC_BYTE_SHIFT = 24
CRC_BIT_COUNT = 8
ASCII_UPPER_A = ord("A")
ASCII_UPPER_Z = ord("Z")
ASCII_CASE_OFFSET = ord("a") - ord("A")


class PfsError(ValueError):
    """Raised when an archive violates the PFS 2 format."""


@dataclass(frozen=True, slots=True)
class _StoredEntry:
    index: int
    crc: int
    offset: int
    size: int
    stored_length: int


@dataclass(frozen=True, slots=True)
class PfsEntry:
    """Describe a named logical member in a PFS archive."""

    name: str
    stored: _StoredEntry


def filename_crc(name: str) -> int:
    """Compute the case-insensitive forward CRC stored for a PFS filename."""
    crc = 0
    for encoded_value in name.encode("latin-1") + b"\0":
        value = encoded_value
        if ASCII_UPPER_A <= value <= ASCII_UPPER_Z:
            value += ASCII_CASE_OFFSET
        crc ^= value << CRC_BYTE_SHIFT
        for _ in range(CRC_BIT_COUNT):
            crc = (
                ((crc << 1) ^ CRC_POLYNOMIAL) & UINT32_MAX
                if crc & CRC_HIGH_BIT
                else (crc << 1) & UINT32_MAX
            )
    return crc


def _compress_payload(payload: bytes) -> bytes:
    output = bytearray()
    for offset in range(0, len(payload), CHUNK_SIZE):
        chunk = payload[offset : offset + CHUNK_SIZE]
        compressed = zlib.compress(chunk, level=PFS_COMPRESSION_LEVEL)
        output.extend(struct.pack("<II", len(compressed), len(chunk)))
        output.extend(compressed)
    return bytes(output)


class PfsArchive:
    """Provide validated access to one immutable PFS archive source."""

    def _validate_chunks(self, offset: int, size: int, directory_offset: int) -> int:
        if not HEADER_SIZE <= offset <= directory_offset:
            msg = "PFS entry offset is outside the data area"
            raise PfsError(msg)
        position = offset
        produced = 0
        while produced < size:
            if position + 8 > directory_offset:
                msg = "PFS chunk header is truncated"
                raise PfsError(msg)
            compressed, uncompressed = struct.unpack_from("<II", self._data, position)
            if compressed == 0 or uncompressed == 0 or produced + uncompressed > size:
                msg = "PFS chunk lengths are invalid"
                raise PfsError(msg)
            position += 8
            if position + compressed > directory_offset:
                msg = "PFS compressed chunk exceeds the data area"
                raise PfsError(msg)
            position += compressed
            produced += uncompressed
        return position - offset

    def _read_directory(self) -> tuple[list[_StoredEntry], int]:
        if len(self._data) < HEADER_SIZE + UINT32_SIZE:
            msg = "Archive is too short to contain a PFS header"
            raise PfsError(msg)

        directory_offset, magic, version = struct.unpack_from("<I4sI", self._data)
        if magic != PFS_MAGIC or version != PFS_VERSION:
            msg = "Only PFS 2 archives are supported"
            raise PfsError(msg)
        if not HEADER_SIZE <= directory_offset <= len(self._data) - UINT32_SIZE:
            msg = "PFS directory offset is outside the archive"
            raise PfsError(msg)

        count = struct.unpack_from("<I", self._data, directory_offset)[0]
        if count == 0 or count > MAX_ENTRY_COUNT:
            msg = f"Invalid PFS directory count: {count}"
            raise PfsError(msg)
        directory_end = directory_offset + UINT32_SIZE + count * DIRECTORY_RECORD_SIZE
        if directory_end > len(self._data):
            msg = "PFS directory is truncated"
            raise PfsError(msg)

        stored: list[_StoredEntry] = []
        for index in range(count):
            crc, offset, size = struct.unpack_from(
                "<III",
                self._data,
                directory_offset + UINT32_SIZE + index * DIRECTORY_RECORD_SIZE,
            )
            stored_length = self._validate_chunks(offset, size, directory_offset)
            stored.append(_StoredEntry(index, crc, offset, size, stored_length))
        return stored, directory_end

    @staticmethod
    def _find_filename_entry(stored: list[_StoredEntry]) -> _StoredEntry:
        filename_entries = [
            entry for entry in stored if entry.crc == FILENAME_DIRECTORY_CRC
        ]
        if not filename_entries:
            filename_entries = [entry for entry in stored if entry.crc == UINT32_MAX]
        if len(filename_entries) != 1:
            msg = "Archive does not contain one filename directory"
            raise PfsError(msg)
        return filename_entries[0]

    @staticmethod
    def _map_entries(
        names: list[str],
        stored: list[_StoredEntry],
        filename_entry: _StoredEntry,
    ) -> list[PfsEntry]:
        data_records = [
            entry for entry in stored if entry.index != filename_entry.index
        ]
        by_crc: dict[int, list[_StoredEntry]] = {}
        for entry in data_records:
            by_crc.setdefault(entry.crc, []).append(entry)
        for candidates in by_crc.values():
            candidates.sort(key=lambda item: item.index)

        mapped: list[PfsEntry | None] = [None] * len(names)
        unmatched_positions: list[int] = []
        for position, name in enumerate(names):
            candidates = by_crc.get(filename_crc(name))
            if candidates:
                mapped[position] = PfsEntry(name, candidates.pop(0))
            else:
                unmatched_positions.append(position)

        unmatched_stored = {
            entry.index: entry for candidates in by_crc.values() for entry in candidates
        }
        for position in unmatched_positions:
            if position >= len(data_records):
                continue
            candidate = data_records[position]
            if unmatched_stored.pop(candidate.index, None) is not None:
                mapped[position] = PfsEntry(names[position], candidate)

        missing_required = any(entry is None for entry in mapped[: len(data_records)])
        if unmatched_stored or missing_required:
            msg = "PFS directory records could not be mapped to filenames"
            raise PfsError(msg)
        return [entry for entry in mapped if entry is not None]

    def _decompress(self, entry: _StoredEntry) -> bytes:
        output = bytearray()
        position = entry.offset
        while len(output) < entry.size:
            compressed, uncompressed = struct.unpack_from("<II", self._data, position)
            position += 8
            decoded = zlib.decompress(self._data[position : position + compressed])
            if len(decoded) != uncompressed:
                msg = "PFS zlib chunk length does not match its header"
                raise PfsError(msg)
            output.extend(decoded)
            position += compressed
        if len(output) != entry.size:
            msg = "PFS entry length does not match its directory record"
            raise PfsError(msg)
        return bytes(output)

    @staticmethod
    def _parse_names(payload: bytes) -> list[str]:
        if len(payload) < UINT32_SIZE:
            msg = "Filename directory is truncated"
            raise PfsError(msg)
        count = struct.unpack_from("<I", payload)[0]
        position = UINT32_SIZE
        names: list[str] = []
        for _ in range(count):
            if position + UINT32_SIZE > len(payload):
                msg = "Filename directory is truncated"
                raise PfsError(msg)
            length = struct.unpack_from("<I", payload, position)[0]
            position += UINT32_SIZE
            encoded = payload[position : position + length]
            position += length
            if not encoded or encoded[-1] != 0 or b"\0" in encoded[:-1]:
                msg = "PFS filename is not null terminated"
                raise PfsError(msg)
            names.append(encoded[:-1].decode("latin-1"))
        if position != len(payload):
            msg = "Filename directory has trailing bytes"
            raise PfsError(msg)
        return names

    def __init__(self, path: Path) -> None:
        """Open and fully validate the archive at *path*."""
        self.path = path.resolve()
        self._data = self.path.read_bytes()
        stored, directory_end = self._read_directory()
        filename_entry = self._find_filename_entry(stored)
        names = self._parse_names(self._decompress(filename_entry))
        entries = self._map_entries(names, stored, filename_entry)

        self._stored = stored
        self._filename_entry = filename_entry
        self._trailing = self._data[directory_end:]
        self.entries = tuple(entries)
        self._entries_by_name = {entry.name.casefold(): entry for entry in entries}

    def read(self, entry: PfsEntry | str) -> bytes:
        """Decompress and return one logical archive member."""
        selected = (
            self._entries_by_name[entry.casefold()] if isinstance(entry, str) else entry
        )
        return self._decompress(selected.stored)

    def rebuild(self, destination: Path, replacements: dict[str, bytes]) -> None:
        """Rebuild to *destination*, replacing only named member payloads."""
        unknown = {name.casefold() for name in replacements} - set(
            self._entries_by_name,
        )
        if unknown:
            msg = f"Replacement entries do not exist: {', '.join(sorted(unknown))}"
            raise PfsError(
                msg,
            )

        replacement_keys = {
            name.casefold(): payload for name, payload in replacements.items()
        }
        logical_by_index = {entry.stored.index: entry for entry in self.entries}
        output = bytearray(struct.pack("<I4sI", 0, PFS_MAGIC, PFS_VERSION))
        records: list[tuple[int, int, int]] = [(0, 0, 0)] * len(self._stored)

        for stored in sorted(self._stored, key=lambda item: (item.offset, item.index)):
            offset = len(output)
            logical = logical_by_index.get(stored.index)
            replacement = (
                replacement_keys.get(logical.name.casefold()) if logical else None
            )
            if replacement is None:
                output.extend(
                    self._data[stored.offset : stored.offset + stored.stored_length],
                )
                size = stored.size
            else:
                output.extend(_compress_payload(replacement))
                size = len(replacement)
            records[stored.index] = (stored.crc, offset, size)

        directory_offset = len(output)
        output.extend(struct.pack("<I", len(records)))
        for record in records:
            output.extend(struct.pack("<III", *record))
        output.extend(self._trailing)
        struct.pack_into("<I", output, 0, directory_offset)

        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_bytes(output)
        temporary.replace(destination)
