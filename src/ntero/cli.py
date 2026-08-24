"""Extract, update, repack, and launch The Game's S3D texture packs."""

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from pathlib import PurePosixPath
from typing import Protocol
from typing import cast

from alive_progress import alive_bar

from ntero.alpha import alpha_mode
from ntero.archive_index import load_manifest_paths
from ntero.archive_index import write_archive_index
from ntero.benchmark import run_benchmark
from ntero.decoder import TextureDecodeError
from ntero.decoder import decode_to_png
from ntero.encoder import TextureEncodeError
from ntero.encoder import encode_png_bytes
from ntero.encoder import encoding_key
from ntero.manifest import MANIFEST_NAME
from ntero.manifest import TextureRecord
from ntero.manifest import load_manifest
from ntero.manifest import write_manifest
from ntero.pack_state import PACK_STATE_NAME
from ntero.pack_state import ArchivePackState
from ntero.pack_state import completed_pack_state
from ntero.pack_state import file_sha256
from ntero.pack_state import load_pack_state
from ntero.pack_state import packed_output_matches
from ntero.pack_state import write_pack_state
from ntero.pfs import PfsArchive
from ntero.resources import resource_path
from ntero.sound import SOUND_MANIFEST_NAME
from ntero.sound import extract_sound_archive
from ntero.sound import pack_sound_manifest
from ntero.sound import update_sound_archive

TEXTURE_EXTENSIONS = {".dds", ".bmp", ".tga"}
GAME_EXECUTABLE_NAME = "eqgame.exe"
UPDATE_LOG_NAME = "update-log.txt"
DEFAULT_WORKERS: int = min(4, os.cpu_count() or 1)
PACK_DEFAULT_WORKERS: int = min(8, os.cpu_count() or 1)
LEGACY_CACHE_INDEX_NAME = "texture-hashes.json"
LEGACY_ENCODED_DIRECTORY_NAME = "encoded"


class _ProjectMetadataError(ValueError):
    """Raised when packaged project metadata is incomplete or malformed."""


class _Progress(Protocol):
    text: str

    def __call__(self, count: int = 1, *, skipped: bool = False) -> None: ...


@dataclass(frozen=True, slots=True)
class _TextureContext:
    archive: PfsArchive
    archive_root: Path
    working: Path


@dataclass(frozen=True, slots=True)
class _PackContext:
    pack_root: Path
    packed_root: Path
    lossy: bool
    pack_states: dict[str, ArchivePackState]


def _write_status(message: str) -> None:
    sys.stdout.write(f"{message}\n")


@contextmanager
def _archive_progress(total: int, title: str) -> Generator[_Progress]:
    progress_context = cast(
        "AbstractContextManager[_Progress]",
        alive_bar(total, title=title, enrich_print=False),
    )
    with progress_context as progress:
        yield progress


def _add_pack_location(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--library-root", type=Path, required=True)
    parser.add_argument("--texture-pack-name")
    parser.add_argument("--sound-pack-name")


def _worker_count(value: str) -> int:
    workers = int(value)
    if workers < 1:
        msg = "workers must be at least 1"
        raise argparse.ArgumentTypeError(msg)
    return workers


def _add_workers(
    parser: argparse.ArgumentParser,
    default: int = DEFAULT_WORKERS,
) -> None:
    parser.add_argument(
        "--workers",
        type=_worker_count,
        default=default,
        help=f"Process archives concurrently (default: {default})",
    )


def _add_benchmark(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Profile a representative sample without changing persistent files",
    )


def _project_version() -> str:
    project_file = resource_path("pyproject.toml")
    with project_file.open("rb") as stream:
        document = cast("dict[object, object]", tomllib.load(stream))
    project = document.get("project")
    if not isinstance(project, dict):
        msg = "pyproject.toml does not contain a [project] table"
        raise _ProjectMetadataError(msg)
    version = cast("dict[object, object]", project).get("version")
    if not isinstance(version, str) or not version:
        msg = "pyproject.toml does not contain a project version"
        raise _ProjectMetadataError(msg)
    return version


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ntero",
        description=__doc__,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_project_version()}",
    )
    parser.set_defaults(benchmark=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser(
        "extract",
        help="Extract editable textures and/or sounds",
    )
    _add_pack_location(extract)
    _add_workers(extract)
    _add_benchmark(extract)
    extract.add_argument("--game-dir", type=Path, required=True)

    update = subparsers.add_parser(
        "update",
        help="Add newly available textures and/or sounds without overwriting edits",
    )
    _add_pack_location(update)
    _add_workers(update)
    _add_benchmark(update)
    update.add_argument("--game-dir", type=Path, required=True)

    pack = subparsers.add_parser(
        "pack",
        help="Rebuild selected texture and/or sound archives",
    )
    _add_pack_location(pack)
    _add_workers(pack, PACK_DEFAULT_WORKERS)
    _add_benchmark(pack)
    pack.add_argument(
        "--lossless",
        action="store_true",
        help="Encode every editable texture as uncompressed BGRA DDS",
    )

    play = subparsers.add_parser(
        "play",
        help="Build an overlay and launch The Game",
    )
    _add_pack_location(play)
    play.add_argument("--game-dir", type=Path, required=True)
    play.add_argument(
        "--no-launch",
        action="store_true",
        help="Create the overlay without launching the game",
    )
    return parser


def _normalize_arguments(arguments: list[str] | None) -> list[str]:
    normalized = list(sys.argv[1:] if arguments is None else arguments)
    if normalized and normalized[0] == "--update":
        normalized[0] = "update"
    return normalized


def _safe_asset_pack_root(
    library_root: Path,
    category: str,
    pack_name: str,
) -> Path:
    if (
        not pack_name.strip()
        or Path(pack_name).name != pack_name
        or pack_name in {".", ".."}
    ):
        msg = "Pack names must be one directory name"
        raise ValueError(msg)
    resolved_library = library_root.resolve()
    pack_root = resolved_library / category / pack_name
    legacy_root = resolved_library / pack_name if category == "textures" else None
    if legacy_root is not None and legacy_root.is_dir() and not pack_root.exists():
        pack_root.parent.mkdir(parents=True, exist_ok=True)
        legacy_root.replace(pack_root)
    return pack_root


def _safe_pack_root(library_root: Path, pack_name: str) -> Path:
    return _safe_asset_pack_root(library_root, "textures", pack_name)


def _safe_sound_pack_root(library_root: Path, pack_name: str) -> Path:
    return _safe_asset_pack_root(library_root, "sounds", pack_name)


def _safe_member_path(name: str) -> Path:
    candidate = PurePosixPath(name.replace("\\", "/"))
    if (
        candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        msg = f"Unsafe archive member path: {name}"
        raise ValueError(msg)
    return Path(*candidate.parts)


def _texture_record(
    archive_root: Path,
    path: Path,
    entry_name: str,
    *,
    special: bool,
) -> TextureRecord:
    return TextureRecord(
        name=entry_name,
        editable=path.relative_to(archive_root).as_posix(),
        special=special,
        alpha=None if special else alpha_mode(path),
    )


def _materialize_texture(
    context: _TextureContext,
    relative_texture: Path,
    entry_name: str,
) -> TextureRecord:
    original = context.archive.read(entry_name)
    temporary = context.working / relative_texture
    editable = context.archive_root / "textures" / relative_texture.with_suffix(".png")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_bytes(original)
    try:
        decode_to_png(temporary, editable)
        return _texture_record(
            context.archive_root,
            editable,
            entry_name,
            special=False,
        )
    except TextureDecodeError:
        editable.unlink(missing_ok=True)
        special = context.archive_root / "special" / relative_texture
        special.parent.mkdir(parents=True, exist_ok=True)
        special.write_bytes(original)
        return _texture_record(
            context.archive_root,
            special,
            entry_name,
            special=True,
        )
    finally:
        temporary.unlink(missing_ok=True)


def _extract_archive(
    archive_path: Path,
    *,
    game_directory: Path,
    pack_root: Path,
) -> tuple[Path, int] | None:
    relative_archive = archive_path.relative_to(game_directory)
    archive = PfsArchive(archive_path)
    textures = [
        entry
        for entry in archive.entries
        if Path(entry.name).suffix.lower() in TEXTURE_EXTENSIONS
    ]
    if not textures:
        return None

    archive_root = pack_root / relative_archive.with_suffix("")
    source_path = archive_root / "source.s3d"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_path, source_path)
    context = _TextureContext(
        archive,
        archive_root,
        archive_root / ".working",
    )
    records: list[TextureRecord] = []
    try:
        for entry in textures:
            relative_texture = _safe_member_path(entry.name)
            records.append(_materialize_texture(context, relative_texture, entry.name))
    finally:
        shutil.rmtree(context.working, ignore_errors=True)
    write_manifest(
        archive_root / MANIFEST_NAME,
        relative_archive.as_posix(),
        records,
    )
    return relative_archive, len(textures)


def _extract(options: argparse.Namespace) -> None:
    game_directory = options.game_dir.resolve()
    pack_root = _safe_pack_root(options.library_root, options.texture_pack_name)
    archives = [
        path
        for path in sorted(
            game_directory.rglob("*.s3d"),
            key=lambda item: str(item).casefold(),
        )
        if path.relative_to(game_directory).as_posix().casefold() != "sky.s3d"
    ]
    process_archive = partial(
        _extract_archive,
        game_directory=game_directory,
        pack_root=pack_root,
    )
    extracted = 0
    manifests: list[Path] = []
    with (
        _archive_progress(len(archives), "Extracting S3D archives") as progress,
        ThreadPoolExecutor(max_workers=options.workers) as executor,
    ):
        futures = [executor.submit(process_archive, archive) for archive in archives]
        for archive, future in zip(archives, futures, strict=True):
            progress.text = str(archive.relative_to(game_directory))
            result = future.result()
            if result is not None:
                relative_archive, texture_count = result
                extracted += texture_count
                manifests.append(relative_archive.with_suffix("") / MANIFEST_NAME)
            progress()
    write_archive_index(pack_root, manifests, MANIFEST_NAME)
    _write_status(f"Extracted {extracted} texture(s) into {pack_root}")


def _texture_pack_inputs(
    records: list[TextureRecord],
    archive_root: Path,
    context: _PackContext,
) -> tuple[dict[str, str], dict[str, Path]]:
    """Fingerprint editable texture inputs."""
    input_hashes: dict[str, str] = {}
    editable_paths: dict[str, Path] = {}
    encoding = encoding_key(lossy=context.lossy)
    for record in records:
        if record.special:
            continue
        name = record.name
        editable = archive_root / record.editable
        if not editable.is_file():
            msg = f"Editable texture is missing: {editable}"
            raise FileNotFoundError(msg)
        editable_paths[name] = editable
        input_hashes[name] = f"{file_sha256(editable)}:{encoding}"
    return input_hashes, editable_paths


def _pack_manifest(
    manifest_path: Path,
    *,
    context: _PackContext,
) -> tuple[Path, int, ArchivePackState]:
    archive_root = manifest_path.parent
    manifest = load_manifest(manifest_path)
    source_path = archive_root / "source.s3d"
    destination = context.packed_root / Path(manifest.archive)
    state_key = manifest_path.relative_to(context.pack_root).as_posix()
    previous_state = context.pack_states.get(state_key)
    source_sha256 = file_sha256(source_path)
    manifest_sha256 = file_sha256(manifest_path)
    input_hashes, editable_paths = _texture_pack_inputs(
        manifest.textures,
        archive_root,
        context,
    )
    baseline_state = (
        previous_state
        if previous_state is not None
        and packed_output_matches(destination, previous_state)
        and previous_state.source_sha256 == source_sha256
        and previous_state.manifest_sha256 == manifest_sha256
        else None
    )
    if baseline_state is not None and baseline_state.inputs == input_hashes:
        return (
            destination.relative_to(context.pack_root),
            0,
            baseline_state,
        )

    changed_names = (
        {
            name
            for name, digest in input_hashes.items()
            if baseline_state.inputs.get(name) != digest
        }
        if baseline_state is not None
        else set(input_hashes)
    )
    baseline = (
        PfsArchive(destination)
        if baseline_state is not None
        else PfsArchive(source_path)
    )
    replacements: dict[str, bytes] = {}
    for record in manifest.textures:
        name = record.name
        if record.special or name not in changed_names:
            continue
        replacements[name] = encode_png_bytes(
            editable_paths[name],
            name,
            lossy=context.lossy,
            expected_alpha=record.alpha,
        )
    baseline.rebuild(destination, replacements)
    state = completed_pack_state(
        source_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
        inputs=input_hashes,
        packed_path=destination,
    )
    return (
        destination.relative_to(context.pack_root),
        len(replacements),
        state,
    )


def _remove_legacy_texture_cache(pack_root: Path, manifests: list[Path]) -> None:
    """Remove encoded payload caches superseded by archive-level state."""
    (pack_root / LEGACY_CACHE_INDEX_NAME).unlink(missing_ok=True)
    for manifest in manifests:
        shutil.rmtree(
            manifest.parent / LEGACY_ENCODED_DIRECTORY_NAME,
            ignore_errors=True,
        )


def _pack(options: argparse.Namespace) -> None:
    pack_root = _safe_pack_root(options.library_root, options.texture_pack_name)
    packed_root = pack_root / "packed"
    state_path = pack_root / PACK_STATE_NAME
    pack_states = load_pack_state(state_path)
    manifests = load_manifest_paths(pack_root, MANIFEST_NAME)
    if not manifests:
        msg = f"No extracted pack manifests found under {pack_root}"
        raise FileNotFoundError(msg)
    _remove_legacy_texture_cache(pack_root, manifests)
    context = _PackContext(
        pack_root=pack_root,
        packed_root=packed_root,
        lossy=not options.lossless,
        pack_states=pack_states,
    )
    process_manifest = partial(_pack_manifest, context=context)
    encoded = 0
    state_updates: dict[str, ArchivePackState] = {}
    try:
        with (
            _archive_progress(len(manifests), "Packing S3D archives") as progress,
            ThreadPoolExecutor(max_workers=options.workers) as executor,
        ):
            futures = [
                executor.submit(process_manifest, manifest) for manifest in manifests
            ]
            for manifest, future in zip(manifests, futures, strict=True):
                progress.text = str(manifest.parent.relative_to(pack_root))
                _, archive_encoded, archive_state = future.result()
                encoded += archive_encoded
                state_key = manifest.relative_to(pack_root).as_posix()
                state_updates[state_key] = archive_state
                progress()
    except Exception:
        state_checkpoint = dict(pack_states)
        state_checkpoint.update(state_updates)
        write_pack_state(state_path, state_checkpoint)
        raise
    write_pack_state(state_path, state_updates)
    _write_status(f"Encoded and packed {encoded} changed texture(s) into {packed_root}")


def _existing_update_record(
    archive: PfsArchive,
    archive_root: Path,
    relative_texture: Path,
    entry_name: str,
    existing: TextureRecord | None,
) -> tuple[TextureRecord | None, bool]:
    if existing is None:
        return None, False
    if existing.special:
        special = archive_root / "special" / relative_texture
        special.parent.mkdir(parents=True, exist_ok=True)
        special.write_bytes(archive.read(entry_name))
        return _texture_record(
            archive_root,
            special,
            entry_name,
            special=True,
        ), True
    existing_path = archive_root / existing.editable
    if not existing_path.is_file():
        return None, False
    if existing.alpha is None:
        return _texture_record(
            archive_root,
            existing_path,
            entry_name,
            special=False,
        ), False
    return existing, False


def _missing_update_record(
    context: _TextureContext,
    relative_texture: Path,
    entry_name: str,
) -> tuple[TextureRecord, bool]:
    editable = context.archive_root / "textures" / relative_texture.with_suffix(".png")
    if editable.is_file():
        return _texture_record(
            context.archive_root,
            editable,
            entry_name,
            special=False,
        ), False

    return _materialize_texture(context, relative_texture, entry_name), True


def _load_existing_records(manifest_path: Path) -> dict[str, TextureRecord]:
    if not manifest_path.is_file():
        return {}
    manifest = load_manifest(manifest_path)
    return {record.name.casefold(): record for record in manifest.textures}


def _refresh_archive_source(
    archive_path: Path,
    archive_root: Path,
    relative_archive: Path,
    records: list[TextureRecord],
) -> None:
    source_path = archive_root / "source.s3d"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_source = source_path.with_name(".source.s3d.tmp")
    shutil.copy2(archive_path, temporary_source)
    temporary_source.replace(source_path)
    write_manifest(
        archive_root / MANIFEST_NAME,
        relative_archive.as_posix(),
        records,
    )


def _update_archive(
    archive_path: Path,
    relative_archive: Path,
    pack_root: Path,
) -> tuple[list[Path], int, bool]:
    archive = PfsArchive(archive_path)
    textures = [
        entry
        for entry in archive.entries
        if Path(entry.name).suffix.lower() in TEXTURE_EXTENSIONS
    ]
    if not textures:
        return [], 0, False

    archive_root = pack_root / relative_archive.with_suffix("")
    existing_records = _load_existing_records(archive_root / MANIFEST_NAME)
    working = archive_root / ".working"
    context = _TextureContext(archive, archive_root, working)
    records: list[TextureRecord] = []
    added_files: list[Path] = []
    refreshed_special = 0
    try:
        for entry in textures:
            relative_texture = _safe_member_path(entry.name)
            record, refreshed = _existing_update_record(
                archive,
                archive_root,
                relative_texture,
                entry.name,
                existing_records.get(entry.name.casefold()),
            )
            refreshed_special += int(refreshed)
            if record is None:
                record, was_added = _missing_update_record(
                    context,
                    relative_texture,
                    entry.name,
                )
                if was_added:
                    added_files.append(
                        (archive_root / record.editable).relative_to(pack_root),
                    )
            records.append(record)
        _refresh_archive_source(
            archive_path,
            archive_root,
            relative_archive,
            records,
        )
    finally:
        shutil.rmtree(working, ignore_errors=True)
    return added_files, refreshed_special, True


def _append_update_log(pack_root: Path, added_files: list[Path]) -> None:
    if not added_files:
        return
    timestamp = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")
    log_path = pack_root / UPDATE_LOG_NAME
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{timestamp}]\n")
        for path in added_files:
            stream.write(f"{path.as_posix()}\n")
        stream.write("\n")


def _update(options: argparse.Namespace) -> None:
    game_directory = options.game_dir.resolve()
    pack_root = _safe_pack_root(options.library_root, options.texture_pack_name)
    if not pack_root.is_dir():
        msg = f"Pack does not exist: {pack_root}"
        raise FileNotFoundError(msg)
    archives = sorted(
        game_directory.rglob("*.s3d"),
        key=lambda path: str(path).casefold(),
    )
    added_files: list[Path] = []
    manifests: list[Path] = []
    refreshed_special = 0
    archives = [
        path
        for path in archives
        if path.relative_to(game_directory).as_posix().casefold() != "sky.s3d"
    ]
    with (
        _archive_progress(len(archives), "Updating S3D archives") as progress,
        ThreadPoolExecutor(max_workers=options.workers) as executor,
    ):
        futures = [
            (
                archive_path.relative_to(game_directory),
                executor.submit(
                    _update_archive,
                    archive_path,
                    archive_path.relative_to(game_directory),
                    pack_root,
                ),
            )
            for archive_path in archives
        ]
        for relative_archive, future in futures:
            progress.text = str(relative_archive)
            archive_added_files, archive_refreshed, has_manifest = future.result()
            added_files.extend(archive_added_files)
            refreshed_special += archive_refreshed
            if has_manifest:
                manifests.append(
                    relative_archive.with_suffix("") / MANIFEST_NAME,
                )
            progress()
    write_archive_index(pack_root, manifests, MANIFEST_NAME)
    for path in added_files:
        _write_status(f"Added file: {path.as_posix()}")
    _append_update_log(pack_root, added_files)
    _write_status(
        f"Added {len(added_files)} new texture(s) without overwriting editable PNGs",
    )
    _write_status(
        f"Refreshed {refreshed_special} special file(s) "
        "from The Game's current archives",
    )


def _replace_with_hardlink(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.samefile(source):
        return
    if destination.is_dir():
        shutil.rmtree(destination)
    temporary = destination.with_name(f".{destination.name}.ntero-link.tmp")
    temporary.unlink(missing_ok=True)
    try:
        os.link(source, temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_stale_overlay_paths(overlay: Path, desired_paths: set[str]) -> None:
    for destination in overlay.rglob("*"):
        relative_key = destination.relative_to(overlay).as_posix().casefold()
        if destination.is_file() and relative_key not in desired_paths:
            destination.unlink()
    directories = sorted(
        (path for path in overlay.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        with suppress(OSError):
            directory.rmdir()


def _sync_overlay_links(
    game_directory: Path,
    packed_roots: list[Path],
    overlay: Path,
) -> None:
    desired: dict[str, tuple[Path, Path]] = {}
    for source in game_directory.rglob("*"):
        if source.is_file():
            relative = source.relative_to(game_directory)
            desired[relative.as_posix().casefold()] = (source, relative)
    for packed_root in packed_roots:
        for source in packed_root.rglob("*"):
            if source.is_file():
                relative = source.relative_to(packed_root)
                desired[relative.as_posix().casefold()] = (source, relative)

    with _archive_progress(len(desired), "Linking overlay files") as progress:
        for source, relative in sorted(
            desired.values(),
            key=lambda item: item[1].as_posix().casefold(),
        ):
            progress.text = str(relative)
            destination = overlay / relative
            _replace_with_hardlink(source, destination)
            progress()

    _remove_stale_overlay_paths(overlay, set(desired))


def _populate_overlay(
    game_directory: Path,
    packed_roots: list[Path],
    overlay: Path,
) -> Path:
    overlay.mkdir(parents=True, exist_ok=True)
    _sync_overlay_links(game_directory, packed_roots, overlay)
    executable = overlay / GAME_EXECUTABLE_NAME
    if not executable.is_file():
        msg = f"Overlay does not contain The Game executable: {executable}"
        raise FileNotFoundError(msg)
    return executable


def _build_overlay(
    game_directory: Path,
    packed_roots: list[Path],
    overlay: Path,
) -> Path:
    return _populate_overlay(game_directory, packed_roots, overlay)


def _launch_game(executable: Path, overlay: Path) -> None:
    subprocess.Popen([str(executable), "patchme"], cwd=overlay)  # noqa: S603


def _require_packed_root(pack_root: Path) -> Path:
    packed_root = pack_root / "packed"
    if not packed_root.is_dir():
        msg = f"Pack has not been built: {packed_root}"
        raise FileNotFoundError(msg)
    return packed_root


def _play(options: argparse.Namespace) -> None:
    game_directory = options.game_dir.resolve()
    packed_roots: list[Path] = []
    if options.texture_pack_name:
        packed_roots.append(
            _require_packed_root(
                _safe_pack_root(options.library_root, options.texture_pack_name),
            ),
        )
    if options.sound_pack_name:
        packed_roots.append(
            _require_packed_root(
                _safe_sound_pack_root(options.library_root, options.sound_pack_name),
            ),
        )
    overlay = options.library_root.resolve() / "overlay"
    executable = _build_overlay(game_directory, packed_roots, overlay)
    _write_status(f"Overlay ready: {overlay}")
    if not options.no_launch:
        _launch_game(executable, overlay)


def _sound_archives(game_directory: Path) -> list[Path]:
    return sorted(game_directory.rglob("*.pfs"), key=lambda path: str(path).casefold())


def _extract_sounds(options: argparse.Namespace) -> None:
    game_directory = options.game_dir.resolve()
    pack_root = _safe_sound_pack_root(options.library_root, options.sound_pack_name)
    archives = _sound_archives(game_directory)
    process = partial(
        extract_sound_archive,
        game_directory=game_directory,
        pack_root=pack_root,
    )
    extracted = 0
    manifests: list[Path] = []
    with (
        _archive_progress(len(archives), "Extracting PFS sounds") as progress,
        ThreadPoolExecutor(max_workers=options.workers) as executor,
    ):
        futures = [executor.submit(process, archive) for archive in archives]
        for archive, future in zip(archives, futures, strict=True):
            progress.text = str(archive.relative_to(game_directory))
            result = future.result()
            if result is not None:
                relative_archive, sound_count = result
                manifests.append(
                    relative_archive.with_suffix("") / SOUND_MANIFEST_NAME,
                )
                extracted += sound_count
            progress()
    write_archive_index(pack_root, manifests, SOUND_MANIFEST_NAME)
    _write_status(f"Extracted {extracted} sound(s) into {pack_root}")


def _update_sounds(options: argparse.Namespace) -> None:
    game_directory = options.game_dir.resolve()
    pack_root = _safe_sound_pack_root(options.library_root, options.sound_pack_name)
    if not pack_root.is_dir():
        msg = f"Pack does not exist: {pack_root}"
        raise FileNotFoundError(msg)
    archives = _sound_archives(game_directory)
    process = partial(
        update_sound_archive,
        game_directory=game_directory,
        pack_root=pack_root,
    )
    added_files: list[Path] = []
    manifests: list[Path] = []
    with (
        _archive_progress(len(archives), "Updating PFS sounds") as progress,
        ThreadPoolExecutor(max_workers=options.workers) as executor,
    ):
        futures = [executor.submit(process, archive) for archive in archives]
        for archive, future in zip(archives, futures, strict=True):
            relative_archive = archive.relative_to(game_directory)
            progress.text = str(relative_archive)
            archive_added, has_manifest = future.result()
            added_files.extend(archive_added)
            if has_manifest:
                manifests.append(
                    relative_archive.with_suffix("") / SOUND_MANIFEST_NAME,
                )
            progress()
    write_archive_index(pack_root, manifests, SOUND_MANIFEST_NAME)
    for path in added_files:
        _write_status(f"Added file: {path.as_posix()}")
    _append_update_log(pack_root, added_files)
    _write_status(f"Added {len(added_files)} new sound(s) without overwriting FLACs")


def _pack_sounds(options: argparse.Namespace) -> None:
    pack_root = _safe_sound_pack_root(options.library_root, options.sound_pack_name)
    packed_root = pack_root / "packed"
    state_path = pack_root / PACK_STATE_NAME
    pack_states = load_pack_state(state_path)
    manifests = load_manifest_paths(pack_root, SOUND_MANIFEST_NAME)
    if not manifests:
        msg = f"No extracted sound manifests found under {pack_root}"
        raise FileNotFoundError(msg)
    packed = 0
    state_updates: dict[str, ArchivePackState] = {}
    try:
        with (
            _archive_progress(len(manifests), "Packing PFS sounds") as progress,
            ThreadPoolExecutor(max_workers=options.workers) as executor,
        ):
            futures = [
                executor.submit(
                    pack_sound_manifest,
                    manifest,
                    packed_root,
                    pack_states.get(manifest.relative_to(pack_root).as_posix()),
                )
                for manifest in manifests
            ]
            for manifest, future in zip(manifests, futures, strict=True):
                progress.text = str(manifest.parent.relative_to(pack_root))
                _, archive_packed, archive_state = future.result()
                packed += archive_packed
                state_key = manifest.relative_to(pack_root).as_posix()
                state_updates[state_key] = archive_state
                progress()
    except Exception:
        checkpoint = dict(pack_states)
        checkpoint.update(state_updates)
        write_pack_state(state_path, checkpoint)
        raise
    write_pack_state(state_path, state_updates)
    _write_status(f"Packed {packed} sound(s) into {packed_root}")


def _require_selected_pack(options: argparse.Namespace) -> None:
    if not options.texture_pack_name and not options.sound_pack_name:
        msg = "Select --texture-pack-name, --sound-pack-name, or both"
        raise ValueError(msg)


def _run_extract(options: argparse.Namespace) -> None:
    if options.texture_pack_name:
        _extract(options)
    if options.sound_pack_name:
        _extract_sounds(options)


def _run_update(options: argparse.Namespace) -> None:
    if options.texture_pack_name:
        _update(options)
    if options.sound_pack_name:
        _update_sounds(options)


def _run_pack(options: argparse.Namespace) -> None:
    if options.texture_pack_name:
        _pack(options)
    if options.sound_pack_name:
        _pack_sounds(options)


def main(arguments: list[str] | None = None) -> int:
    """Run the command-line interface and return its process exit code."""
    parser = _create_parser()
    options = parser.parse_args(_normalize_arguments(arguments))
    try:
        _require_selected_pack(options)
        if options.benchmark:
            run_benchmark(options)
        elif options.command == "extract":
            _run_extract(options)
        elif options.command == "update":
            _run_update(options)
        elif options.command == "pack":
            _run_pack(options)
        else:
            _play(options)
    except (OSError, ValueError, TextureEncodeError) as error:
        parser.exit(1, f"error: {error}\n")
    return 0
