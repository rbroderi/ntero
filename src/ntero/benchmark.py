"""Run representative command workloads without changing persistent files."""

import argparse
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from ntero.alpha import alpha_mode
from ntero.archive_index import load_manifest_paths
from ntero.decoder import TextureDecodeError
from ntero.decoder import decode_to_png
from ntero.encoder import encode_png_bytes
from ntero.manifest import MANIFEST_NAME
from ntero.manifest import TextureRecord
from ntero.manifest import load_manifest
from ntero.manifest import write_manifest
from ntero.pfs import PfsArchive
from ntero.pfs import PfsEntry
from ntero.sound import FLAC_EXTENSION
from ntero.sound import SOUND_MANIFEST_NAME
from ntero.sound import SoundRecord
from ntero.sound import decode_wav_to_flac
from ntero.sound import encode_flac_to_wav
from ntero.sound import load_sound_manifest
from ntero.sound import write_sound_manifest

ARCHIVE_SAMPLE_LIMIT = 8
MEMBER_SAMPLE_LIMIT = 32
PACK_REPETITIONS = 2
TEXTURE_EXTENSIONS = {".dds", ".bmp", ".tga"}
WAV_EXTENSION = ".wav"


def _sample[T](items: list[T], limit: int) -> list[T]:
    """Select deterministic items spread across an ordered collection."""
    if len(items) <= limit:
        return items
    return [items[index * len(items) // limit] for index in range(limit)]


def _sample_texture_entries(entries: list[PfsEntry]) -> list[PfsEntry]:
    """Sample entries while including each available texture extension."""
    selected: list[PfsEntry] = []
    for extension in sorted(TEXTURE_EXTENSIONS):
        match = next(
            (
                entry
                for entry in entries
                if Path(entry.name).suffix.lower() == extension
            ),
            None,
        )
        if match is not None:
            selected.append(match)
    remaining = [entry for entry in entries if entry not in selected]
    selected.extend(_sample(remaining, MEMBER_SAMPLE_LIMIT - len(selected)))
    return selected


def _safe_member_path(name: str) -> Path:
    normalized = Path(name.replace("\\", "/"))
    if normalized.is_absolute() or any(
        part in {"", ".", ".."} for part in normalized.parts
    ):
        msg = f"Unsafe archive member path: {name}"
        raise ValueError(msg)
    return normalized


def _readonly_pack_root(library_root: Path, category: str, pack_name: str) -> Path:
    if (
        not pack_name.strip()
        or Path(pack_name).name != pack_name
        or pack_name in {".", ".."}
    ):
        msg = "Pack names must be one directory name"
        raise ValueError(msg)
    library = library_root.resolve()
    categorized = library / category / pack_name
    legacy = library / pack_name if category == "textures" else None
    if categorized.is_dir() or legacy is None or not legacy.is_dir():
        return categorized
    return legacy


def _benchmark_texture_extract_archive(
    archive_path: Path,
    destination: Path,
) -> int:
    archive = PfsArchive(archive_path)
    entries = [
        entry
        for entry in archive.entries
        if Path(entry.name).suffix.lower() in TEXTURE_EXTENSIONS
    ]
    selected = _sample_texture_entries(entries)
    if not selected:
        return 0
    archive_root = destination / archive_path.stem
    archive_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_path, archive_root / "source.s3d")
    for entry in selected:
        relative = _safe_member_path(entry.name)
        packed = archive_root / "members" / relative
        editable = archive_root / "textures" / relative.with_suffix(".png")
        packed.parent.mkdir(parents=True, exist_ok=True)
        packed.write_bytes(archive.read(entry))
        try:
            decode_to_png(packed, editable)
        except TextureDecodeError:
            special = archive_root / "special" / relative
            special.parent.mkdir(parents=True, exist_ok=True)
            packed.replace(special)
        else:
            packed.unlink()
    return len(selected)


def _benchmark_sound_extract_archive(
    archive_path: Path,
    destination: Path,
) -> int:
    archive = PfsArchive(archive_path)
    entries = [
        entry
        for entry in archive.entries
        if Path(entry.name).suffix.lower() == WAV_EXTENSION
    ]
    selected = _sample(entries, MEMBER_SAMPLE_LIMIT)
    if not selected:
        return 0
    archive_root = destination / archive_path.stem
    archive_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_path, archive_root / "source.pfs")
    for entry in selected:
        output = (
            archive_root
            / "sounds"
            / _safe_member_path(entry.name).with_suffix(FLAC_EXTENSION)
        )
        decode_wav_to_flac(archive.read(entry), output)
    return len(selected)


def _benchmark_extract(options: argparse.Namespace, workspace: Path) -> int:
    game_directory = options.game_dir.resolve()
    processed = 0
    with ThreadPoolExecutor(max_workers=options.workers) as executor:
        if options.texture_pack_name:
            archives = _sample(
                [
                    path
                    for path in sorted(
                        game_directory.rglob("*.s3d"),
                        key=lambda item: str(item).casefold(),
                    )
                    if path.relative_to(game_directory).as_posix().casefold()
                    != "sky.s3d"
                ],
                ARCHIVE_SAMPLE_LIMIT,
            )
            processed += sum(
                executor.map(
                    _benchmark_texture_extract_archive,
                    archives,
                    [
                        workspace / "textures" / str(index)
                        for index in range(len(archives))
                    ],
                ),
            )
        if options.sound_pack_name:
            archives = _sample(
                sorted(
                    game_directory.rglob("*.pfs"),
                    key=lambda item: str(item).casefold(),
                ),
                ARCHIVE_SAMPLE_LIMIT,
            )
            processed += sum(
                executor.map(
                    _benchmark_sound_extract_archive,
                    archives,
                    [
                        workspace / "sounds" / str(index)
                        for index in range(len(archives))
                    ],
                ),
            )
    return processed


def _benchmark_texture_update_archive(
    archive_path: Path,
    relative_archive: Path,
    pack_root: Path,
    destination: Path,
) -> int:
    archive = PfsArchive(archive_path)
    entries = _sample_texture_entries(
        [
            entry
            for entry in archive.entries
            if Path(entry.name).suffix.lower() in TEXTURE_EXTENSIONS
        ],
    )
    if not entries:
        return 0
    source_root = pack_root / relative_archive.with_suffix("")
    manifest_path = source_root / MANIFEST_NAME
    existing = (
        {
            record.name.casefold(): record
            for record in load_manifest(manifest_path).textures
        }
        if manifest_path.is_file()
        else {}
    )
    archive_root = destination / relative_archive.with_suffix("")
    archive_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_path, archive_root / "source.s3d")
    records: list[TextureRecord] = []
    for entry in entries:
        relative = _safe_member_path(entry.name)
        current = existing.get(entry.name.casefold())
        current_editable = (
            source_root / current.editable if current is not None else None
        )
        if current is not None and current.special:
            special = archive_root / "special" / relative
            special.parent.mkdir(parents=True, exist_ok=True)
            special.write_bytes(archive.read(entry))
            records.append(
                TextureRecord(
                    name=entry.name,
                    editable=special.relative_to(archive_root).as_posix(),
                    special=True,
                ),
            )
            continue
        if current_editable is not None and current_editable.is_file():
            if current is None:
                msg = "Existing editable path requires a manifest record"
                raise RuntimeError(msg)
            copied = archive_root / "textures" / relative.with_suffix(".png")
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current_editable, copied)
            recorded_alpha = current.alpha
            records.append(
                TextureRecord(
                    name=entry.name,
                    editable=copied.relative_to(archive_root).as_posix(),
                    special=False,
                    alpha=(
                        recorded_alpha
                        if recorded_alpha is not None
                        else alpha_mode(current_editable)
                    ),
                ),
            )
            continue
        packed = archive_root / "members" / relative
        editable = archive_root / "textures" / relative.with_suffix(".png")
        packed.parent.mkdir(parents=True, exist_ok=True)
        packed.write_bytes(archive.read(entry))
        try:
            decode_to_png(packed, editable)
        except TextureDecodeError:
            special = archive_root / "special" / relative
            special.parent.mkdir(parents=True, exist_ok=True)
            packed.replace(special)
            records.append(
                TextureRecord(
                    name=entry.name,
                    editable=special.relative_to(archive_root).as_posix(),
                    special=True,
                ),
            )
        else:
            packed.unlink()
            records.append(
                TextureRecord(
                    name=entry.name,
                    editable=editable.relative_to(archive_root).as_posix(),
                    special=False,
                    alpha=alpha_mode(editable),
                ),
            )
    write_manifest(
        archive_root / MANIFEST_NAME,
        relative_archive.as_posix(),
        records,
    )
    return len(entries)


def _benchmark_sound_update_archive(
    archive_path: Path,
    relative_archive: Path,
    pack_root: Path,
    destination: Path,
) -> int:
    archive = PfsArchive(archive_path)
    entries = _sample(
        [
            entry
            for entry in archive.entries
            if Path(entry.name).suffix.lower() == WAV_EXTENSION
        ],
        MEMBER_SAMPLE_LIMIT,
    )
    if not entries:
        return 0
    source_root = pack_root / relative_archive.with_suffix("")
    manifest_path = source_root / SOUND_MANIFEST_NAME
    existing = (
        {
            record.name.casefold(): record
            for record in load_sound_manifest(manifest_path).sounds
        }
        if manifest_path.is_file()
        else {}
    )
    archive_root = destination / relative_archive.with_suffix("")
    archive_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_path, archive_root / "source.pfs")
    records: list[SoundRecord] = []
    for entry in entries:
        current = existing.get(entry.name.casefold())
        current_editable = (
            source_root / current.editable if current is not None else None
        )
        editable = (
            archive_root
            / "sounds"
            / _safe_member_path(entry.name).with_suffix(FLAC_EXTENSION)
        )
        editable.parent.mkdir(parents=True, exist_ok=True)
        if (
            current is not None
            and current_editable is not None
            and current_editable.is_file()
        ):
            shutil.copy2(current_editable, editable)
            profile = current
        else:
            profile = decode_wav_to_flac(archive.read(entry), editable)
        records.append(
            SoundRecord(
                name=entry.name,
                editable=editable.relative_to(archive_root).as_posix(),
                sample_rate=profile.sample_rate,
                channels=profile.channels,
                bits_per_sample=profile.bits_per_sample,
            ),
        )
    write_sound_manifest(
        archive_root / SOUND_MANIFEST_NAME,
        relative_archive.as_posix(),
        records,
    )
    return len(entries)


def _benchmark_update(options: argparse.Namespace, workspace: Path) -> int:
    game_directory = options.game_dir.resolve()
    processed = 0
    with ThreadPoolExecutor(max_workers=options.workers) as executor:
        if options.texture_pack_name:
            pack_root = _readonly_pack_root(
                options.library_root,
                "textures",
                options.texture_pack_name,
            )
            archives = _sample(
                [
                    path
                    for path in sorted(
                        game_directory.rglob("*.s3d"),
                        key=lambda item: str(item).casefold(),
                    )
                    if path.relative_to(game_directory).as_posix().casefold()
                    != "sky.s3d"
                ],
                ARCHIVE_SAMPLE_LIMIT,
            )
            processed += sum(
                executor.map(
                    _benchmark_texture_update_archive,
                    archives,
                    [path.relative_to(game_directory) for path in archives],
                    [pack_root] * len(archives),
                    [workspace / "textures"] * len(archives),
                ),
            )
        if options.sound_pack_name:
            pack_root = _readonly_pack_root(
                options.library_root,
                "sounds",
                options.sound_pack_name,
            )
            archives = _sample(
                sorted(
                    game_directory.rglob("*.pfs"),
                    key=lambda item: str(item).casefold(),
                ),
                ARCHIVE_SAMPLE_LIMIT,
            )
            processed += sum(
                executor.map(
                    _benchmark_sound_update_archive,
                    archives,
                    [path.relative_to(game_directory) for path in archives],
                    [pack_root] * len(archives),
                    [workspace / "sounds"] * len(archives),
                ),
            )
    return processed


def _sample_texture_records(records: list[TextureRecord]) -> list[TextureRecord]:
    entries = [record for record in records if not record.special]
    selected: list[TextureRecord] = []
    for extension in sorted(TEXTURE_EXTENSIONS):
        match = next(
            (
                record
                for record in entries
                if Path(record.name).suffix.lower() == extension
            ),
            None,
        )
        if match is not None:
            selected.append(match)
    remaining = [record for record in entries if record not in selected]
    selected.extend(_sample(remaining, MEMBER_SAMPLE_LIMIT - len(selected)))
    return selected


def _benchmark_texture_pack_manifest(
    manifest_path: Path,
    destination: Path,
    *,
    lossy: bool,
) -> int:
    archive_root = manifest_path.parent
    manifest = load_manifest(manifest_path)
    source = PfsArchive(archive_root / "source.s3d")
    replacements: dict[str, bytes] = {}
    for record in _sample_texture_records(manifest.textures):
        editable = archive_root / record.editable
        if not editable.is_file():
            continue
        name = record.name
        replacements[name] = encode_png_bytes(
            editable,
            name,
            lossy=lossy,
            expected_alpha=record.alpha,
        )
    if replacements:
        source.rebuild(
            destination / "packed" / Path(manifest.archive),
            replacements,
        )
    return len(replacements)


def _benchmark_sound_pack_manifest(manifest_path: Path, destination: Path) -> int:
    archive_root = manifest_path.parent
    manifest = load_sound_manifest(manifest_path)
    source = PfsArchive(archive_root / "source.pfs")
    replacements: dict[str, bytes] = {}
    for record in _sample(manifest.sounds, MEMBER_SAMPLE_LIMIT):
        editable = archive_root / record.editable
        if editable.is_file():
            replacements[record.name] = encode_flac_to_wav(editable, record)
    if replacements:
        source.rebuild(
            destination / "packed" / Path(manifest.archive),
            replacements,
        )
    return len(replacements)


def _benchmark_pack(options: argparse.Namespace, workspace: Path) -> int:
    processed = 0
    with ThreadPoolExecutor(max_workers=options.workers) as executor:
        if options.texture_pack_name:
            pack_root = _readonly_pack_root(
                options.library_root,
                "textures",
                options.texture_pack_name,
            )
            manifests = _sample(
                load_manifest_paths(
                    pack_root,
                    MANIFEST_NAME,
                    persist_discovery=False,
                ),
                ARCHIVE_SAMPLE_LIMIT,
            )
            process_texture_manifest = partial(
                _benchmark_texture_pack_manifest,
                lossy=not options.lossless,
            )
            for _ in range(PACK_REPETITIONS):
                processed += sum(
                    executor.map(
                        process_texture_manifest,
                        manifests,
                        [workspace / "textures"] * len(manifests),
                    ),
                )
        if options.sound_pack_name:
            pack_root = _readonly_pack_root(
                options.library_root,
                "sounds",
                options.sound_pack_name,
            )
            manifests = _sample(
                load_manifest_paths(
                    pack_root,
                    SOUND_MANIFEST_NAME,
                    persist_discovery=False,
                ),
                ARCHIVE_SAMPLE_LIMIT,
            )
            for _ in range(PACK_REPETITIONS):
                processed += sum(
                    executor.map(
                        _benchmark_sound_pack_manifest,
                        manifests,
                        [workspace / "sounds"] * len(manifests),
                    ),
                )
    return processed


def run_benchmark(options: argparse.Namespace) -> None:
    """Run one sampled workflow entirely inside a temporary directory."""
    with tempfile.TemporaryDirectory(prefix="ntero-benchmark-") as temporary:
        workspace = Path(temporary)
        if options.command == "extract":
            processed = _benchmark_extract(options, workspace)
        elif options.command == "update":
            processed = _benchmark_update(options, workspace)
        elif options.command == "pack":
            processed = _benchmark_pack(options, workspace)
        else:
            msg = f"Benchmark mode is not supported for {options.command}"
            raise ValueError(msg)
        if processed == 0:
            msg = f"No benchmarkable items found for {options.command}"
            raise FileNotFoundError(msg)
        print(
            f"Benchmarked {processed} sampled item(s) for {options.command}; "
            "temporary output removed",
        )
