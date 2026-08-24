"""Migrate schema-1 NTERO sound packs from editable WAV to FLAC."""

import argparse
import json
from pathlib import Path, PurePosixPath
from typing import cast

from ntero.sound import (
    SOUND_MANIFEST_NAME,
    SOUND_SCHEMA_VERSION,
    SoundProfile,
    SoundRecord,
    decode_wav_to_flac,
    write_sound_manifest,
)

LEGACY_SCHEMA_VERSION = 1


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"Legacy sound manifest {field} must be a non-empty string"
        raise ValueError(msg)
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        msg = f"Legacy sound manifest {field} must be a safe relative path"
        raise ValueError(msg)
    return value


def _legacy_manifest(path: Path) -> tuple[str, list[tuple[str, str]]] | None:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        msg = f"Legacy sound manifest root must be an object: {path}"
        raise TypeError(msg)
    root = cast("dict[object, object]", value)
    schema_version = root.get("schemaVersion")
    if schema_version == SOUND_SCHEMA_VERSION:
        return None
    if schema_version != LEGACY_SCHEMA_VERSION:
        msg = f"Unsupported sound manifest schemaVersion in {path}"
        raise ValueError(msg)
    raw_sounds = root.get("sounds")
    if not isinstance(raw_sounds, list):
        msg = f"Legacy sound manifest sounds must be an array: {path}"
        raise TypeError(msg)
    sounds: list[tuple[str, str]] = []
    for index, raw_record in enumerate(cast("list[object]", raw_sounds)):
        if not isinstance(raw_record, dict):
            msg = f"Legacy sound manifest sounds[{index}] must be an object"
            raise TypeError(msg)
        record = cast("dict[object, object]", raw_record)
        sounds.append(
            (
                _safe_relative(record.get("name"), f"sounds[{index}].name"),
                _safe_relative(
                    record.get("editable"),
                    f"sounds[{index}].editable",
                ),
            ),
        )
    archive = _safe_relative(root.get("archive"), "archive")
    return archive, sounds


def migrate_manifest(path: Path) -> int:
    """Migrate one legacy manifest and return its converted sound count."""
    legacy = _legacy_manifest(path)
    if legacy is None:
        return 0
    archive, sounds = legacy
    archive_root = path.parent
    candidates: list[tuple[str, Path, Path, Path]] = []
    destinations: set[Path] = set()
    for name, relative_editable in sounds:
        source = archive_root / Path(relative_editable)
        if not source.is_file():
            msg = f"Legacy editable sound is missing: {source}"
            raise FileNotFoundError(msg)
        destination = source.with_suffix(".flac")
        if destination in destinations or destination.exists():
            msg = f"Migration destination already exists: {destination}"
            raise FileExistsError(msg)
        destinations.add(destination)
        temporary = destination.with_name(f".{destination.name}.migration.tmp")
        candidates.append((name, source, temporary, destination))

    staged: list[tuple[Path, Path, Path, str, SoundProfile]] = []
    try:
        for name, source, temporary, destination in candidates:
            profile = decode_wav_to_flac(source.read_bytes(), temporary)
            staged.append((source, temporary, destination, name, profile))

        for _source, temporary, destination, _name, _profile in staged:
            temporary.replace(destination)
        records = [
            SoundRecord(
                name=name,
                editable=destination.relative_to(archive_root).as_posix(),
                sample_rate=profile.sample_rate,
                channels=profile.channels,
                bits_per_sample=profile.bits_per_sample,
            )
            for _source, _temporary, destination, name, profile in staged
        ]
        write_sound_manifest(path, archive, records)
    except Exception:
        for _source, temporary, destination, _name, _profile in staged:
            temporary.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
        raise

    for source, _temporary, _destination, _name, _profile in staged:
        source.unlink()
    return len(staged)


def migrate_library(library_root: Path, sound_pack_name: str | None = None) -> int:
    """Migrate legacy manifests in one sound pack or the entire library."""
    sounds_root = library_root.resolve() / "sounds"
    search_root = sounds_root / sound_pack_name if sound_pack_name else sounds_root
    if not search_root.is_dir():
        msg = f"Sound pack directory does not exist: {search_root}"
        raise FileNotFoundError(msg)
    converted = 0
    for manifest in sorted(search_root.rglob(SOUND_MANIFEST_NAME)):
        count = migrate_manifest(manifest)
        if count:
            print(f"Migrated {count} sound(s): {manifest}")
            converted += count
    return converted


def main() -> int:
    """Run the one-off sound-pack migration command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--sound-pack-name")
    options = parser.parse_args()
    converted = migrate_library(options.library_root, options.sound_pack_name)
    print(f"Migrated {converted} sound(s) total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
