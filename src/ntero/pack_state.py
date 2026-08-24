"""Persist fingerprints needed for incremental archive packing."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

PACK_STATE_NAME = "pack-state.json"
PACK_STATE_SCHEMA_VERSION = 1


def file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ArchivePackState:
    """Describe the inputs and output of one successfully packed archive."""

    source_sha256: str
    manifest_sha256: str
    inputs: dict[str, str]
    packed_size: int
    packed_mtime_ns: int


@dataclass(frozen=True, slots=True)
class PackStateDocument:
    """Store incremental state for every archive in one asset pack."""

    schema_version: int
    archives: dict[str, ArchivePackState]


def _safe_key(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        return None
    return candidate.as_posix()


def load_pack_state(path: Path) -> dict[str, ArchivePackState]:
    """Load valid incremental archive state, ignoring malformed records."""
    if not path.is_file():
        return {}
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        return {}
    root = cast("dict[object, object]", value)
    if root.get("schemaVersion") != PACK_STATE_SCHEMA_VERSION:
        return {}
    raw_archives = root.get("archives")
    if not isinstance(raw_archives, dict):
        return {}

    archives: dict[str, ArchivePackState] = {}
    for raw_key, raw_state in cast("dict[object, object]", raw_archives).items():
        key = _safe_key(raw_key)
        if key is None or not isinstance(raw_state, dict):
            continue
        state = cast("dict[object, object]", raw_state)
        source_sha256 = state.get("sourceSha256")
        manifest_sha256 = state.get("manifestSha256")
        packed_size = state.get("packedSize")
        packed_mtime_ns = state.get("packedMtimeNs")
        raw_inputs = state.get("inputs")
        if (
            not isinstance(source_sha256, str)
            or not isinstance(manifest_sha256, str)
            or type(packed_size) is not int
            or type(packed_mtime_ns) is not int
            or not isinstance(raw_inputs, dict)
        ):
            continue
        typed_inputs = cast("dict[object, object]", raw_inputs)
        inputs = {
            key: value
            for key, value in typed_inputs.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if len(inputs) != len(typed_inputs):
            continue
        archives[key] = ArchivePackState(
            source_sha256=source_sha256,
            manifest_sha256=manifest_sha256,
            inputs=inputs,
            packed_size=packed_size,
            packed_mtime_ns=packed_mtime_ns,
        )
    return archives


def write_pack_state(path: Path, archives: dict[str, ArchivePackState]) -> None:
    """Atomically replace incremental state after successful archive builds."""
    document = PackStateDocument(
        schema_version=PACK_STATE_SCHEMA_VERSION,
        archives=dict(sorted(archives.items())),
    )
    serialized_archives = {
        key: {
            "sourceSha256": state.source_sha256,
            "manifestSha256": state.manifest_sha256,
            "inputs": state.inputs,
            "packedSize": state.packed_size,
            "packedMtimeNs": state.packed_mtime_ns,
        }
        for key, state in document.archives.items()
    }
    serialized = {
        "schemaVersion": document.schema_version,
        "archives": serialized_archives,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(serialized, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def packed_output_matches(path: Path, state: ArchivePackState | None) -> bool:
    """Return whether an existing output still matches its recorded metadata."""
    if state is None:
        return False
    try:
        metadata = path.stat()
    except OSError:
        return False
    return (
        metadata.st_size == state.packed_size
        and metadata.st_mtime_ns == state.packed_mtime_ns
    )


def completed_pack_state(
    *,
    source_sha256: str,
    manifest_sha256: str,
    inputs: dict[str, str],
    packed_path: Path,
) -> ArchivePackState:
    """Create state for one atomically committed packed archive."""
    metadata = packed_path.stat()
    return ArchivePackState(
        source_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
        inputs=dict(sorted(inputs.items())),
        packed_size=metadata.st_size,
        packed_mtime_ns=metadata.st_mtime_ns,
    )
