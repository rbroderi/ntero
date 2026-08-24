"""Persist and resolve the manifests owned by one asset pack."""

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

ARCHIVE_INDEX_NAME = "archive-index.json"
ARCHIVE_INDEX_SCHEMA_VERSION = 1
_PAYLOAD_DIRECTORIES = {
    ".working",
    "encoded",
    "packed",
    "sounds",
    "special",
    "textures",
}


class ArchiveIndexError(ValueError):
    """Raised when an archive index is malformed or stale."""


@dataclass(frozen=True, slots=True)
class ArchiveIndex:
    """List the current manifests in one texture or sound pack."""

    schema_version: int
    manifests: list[str]


def _safe_manifest_path(value: object, manifest_name: str) -> Path:
    if not isinstance(value, str) or not value:
        msg = "Archive index manifests must contain non-empty strings"
        raise ArchiveIndexError(msg)
    candidate = PurePosixPath(value.replace("\\", "/"))
    if (
        candidate.is_absolute()
        or candidate.name != manifest_name
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        msg = f"Archive index manifest path is unsafe: {value}"
        raise ArchiveIndexError(msg)
    return Path(*candidate.parts)


def write_archive_index(
    pack_root: Path,
    manifest_paths: list[Path],
    manifest_name: str,
) -> None:
    """Atomically replace a pack's current manifest index."""
    relative_paths = {
        _safe_manifest_path(path.as_posix(), manifest_name).as_posix()
        for path in manifest_paths
    }
    document = ArchiveIndex(
        schema_version=ARCHIVE_INDEX_SCHEMA_VERSION,
        manifests=sorted(relative_paths, key=str.casefold),
    )
    pack_root.mkdir(parents=True, exist_ok=True)
    destination = pack_root / ARCHIVE_INDEX_NAME
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schemaVersion": document.schema_version,
                "manifests": document.manifests,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def discover_manifest_paths(pack_root: Path, manifest_name: str) -> list[Path]:
    """Find legacy manifests while pruning known archive payload directories."""
    discovered: list[Path] = []
    if not pack_root.is_dir():
        return discovered
    for directory, directories, files in os.walk(pack_root):
        if Path(directory) == pack_root:
            directories[:] = [name for name in directories if name != "packed"]
        if manifest_name in files:
            manifest = Path(directory) / manifest_name
            discovered.append(manifest.relative_to(pack_root))
            directories[:] = [
                name for name in directories if name not in _PAYLOAD_DIRECTORIES
            ]
    return sorted(discovered, key=lambda path: path.as_posix().casefold())


def load_manifest_paths(
    pack_root: Path,
    manifest_name: str,
    *,
    persist_discovery: bool = True,
) -> list[Path]:
    """Load indexed manifests, discovering legacy packs once when necessary."""
    index_path = pack_root / ARCHIVE_INDEX_NAME
    if not index_path.is_file():
        relative_paths = discover_manifest_paths(pack_root, manifest_name)
        if relative_paths and persist_discovery:
            write_archive_index(pack_root, relative_paths, manifest_name)
        return [pack_root / path for path in relative_paths]

    value: object = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        msg = "Archive index root must be an object"
        raise ArchiveIndexError(msg)
    root = cast("dict[object, object]", value)
    if root.get("schemaVersion") != ARCHIVE_INDEX_SCHEMA_VERSION:
        msg = f"Archive index schemaVersion must be {ARCHIVE_INDEX_SCHEMA_VERSION}"
        raise ArchiveIndexError(msg)
    raw_manifests = root.get("manifests")
    if not isinstance(raw_manifests, list):
        msg = "Archive index manifests must be an array"
        raise ArchiveIndexError(msg)

    relative_paths = [
        _safe_manifest_path(item, manifest_name)
        for item in cast("list[object]", raw_manifests)
    ]
    if len(set(relative_paths)) != len(relative_paths):
        msg = "Archive index contains duplicate manifest paths"
        raise ArchiveIndexError(msg)
    manifests = [pack_root / path for path in relative_paths]
    missing = [path for path in manifests if not path.is_file()]
    if missing:
        relative = missing[0].relative_to(pack_root).as_posix()
        msg = f"Indexed manifest is missing: {relative}"
        raise ArchiveIndexError(msg)
    return manifests
