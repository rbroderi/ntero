"""Extract archived WAV members to FLAC and rebuild compatible PCM WAVs."""

import io
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import soundfile

from ntero.pack_state import (
    ArchivePackState,
    completed_pack_state,
    file_sha256,
    packed_output_matches,
)
from ntero.pfs import PfsArchive

SOUND_MANIFEST_NAME = "sound-manifest.json"
SOUND_SCHEMA_VERSION = 2
WAV_EXTENSION = ".wav"
FLAC_EXTENSION = ".flac"
SUPPORTED_BITS_PER_SAMPLE = {8, 16}
EIGHT_BIT_PCM = 8


class SoundManifestError(ValueError):
    """Raised when a sound manifest is malformed."""


@dataclass(frozen=True, slots=True)
class SoundProfile:
    """Describe the archived PCM format that packing must reproduce."""

    sample_rate: int
    channels: int
    bits_per_sample: int


@dataclass(frozen=True, slots=True)
class SoundRecord:
    """Map one archived WAV member to its editable lossless file."""

    name: str
    editable: str
    sample_rate: int
    channels: int
    bits_per_sample: int


@dataclass(frozen=True, slots=True)
class SoundManifest:
    """Describe editable WAV members from one PFS archive."""

    schema_version: int
    archive: str
    sounds: list[SoundRecord]


def _safe_relative(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"Sound manifest {field} must be a non-empty string"
        raise SoundManifestError(msg)
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        msg = f"Sound manifest {field} must be a safe relative path"
        raise SoundManifestError(msg)
    return value


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        msg = f"Sound manifest {field} must be a positive integer"
        raise SoundManifestError(msg)
    return value


def load_sound_manifest(path: Path) -> SoundManifest:
    """Load and validate one sound manifest."""
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        msg = "Sound manifest root must be an object"
        raise SoundManifestError(msg)
    root = cast("dict[object, object]", value)
    if root.get("schemaVersion") != SOUND_SCHEMA_VERSION:
        msg = f"Sound manifest schemaVersion must be {SOUND_SCHEMA_VERSION}"
        raise SoundManifestError(msg)
    raw_sounds = root.get("sounds")
    if not isinstance(raw_sounds, list):
        msg = "Sound manifest sounds must be an array"
        raise SoundManifestError(msg)
    sounds: list[SoundRecord] = []
    for index, raw_record in enumerate(cast("list[object]", raw_sounds)):
        if not isinstance(raw_record, dict):
            msg = f"Sound manifest sounds[{index}] must be an object"
            raise SoundManifestError(msg)
        record = cast("dict[object, object]", raw_record)
        bits_per_sample = record.get("bitsPerSample")
        if bits_per_sample not in SUPPORTED_BITS_PER_SAMPLE:
            msg = f"Sound manifest sounds[{index}].bitsPerSample must be 8 or 16"
            raise SoundManifestError(msg)
        sounds.append(
            SoundRecord(
                name=_safe_relative(record.get("name"), f"sounds[{index}].name"),
                editable=_safe_relative(
                    record.get("editable"),
                    f"sounds[{index}].editable",
                ),
                sample_rate=_positive_int(
                    record.get("sampleRate"),
                    f"sounds[{index}].sampleRate",
                ),
                channels=_positive_int(
                    record.get("channels"),
                    f"sounds[{index}].channels",
                ),
                bits_per_sample=cast("int", bits_per_sample),
            ),
        )
    return SoundManifest(
        schema_version=SOUND_SCHEMA_VERSION,
        archive=_safe_relative(root.get("archive"), "archive"),
        sounds=sounds,
    )


def decode_wav_to_flac(payload: bytes, destination: Path) -> SoundProfile:
    """Decode a PCM WAV payload to editable FLAC and return its audio profile."""
    try:
        with soundfile.SoundFile(io.BytesIO(payload)) as source:
            if source.format != "WAV" or source.subtype not in {"PCM_U8", "PCM_16"}:
                msg = (
                    f"Unsupported archived WAV format: {source.format}/{source.subtype}"
                )
                raise ValueError(msg)
            samples = source.buffer_read(source.frames, dtype="int16")
            sample_rate = source.samplerate
            channels = source.channels
            bits_per_sample = 8 if source.subtype == "PCM_U8" else 16
        destination.parent.mkdir(parents=True, exist_ok=True)
        with soundfile.SoundFile(
            destination,
            mode="w",
            samplerate=sample_rate,
            channels=channels,
            format="FLAC",
            subtype="PCM_16",
        ) as output:
            output.buffer_write(bytes(samples), dtype="int16")
    except soundfile.SoundFileError as error:
        msg = f"Cannot decode PCM WAV: {error}"
        raise ValueError(msg) from error
    return SoundProfile(
        sample_rate=sample_rate,
        channels=channels,
        bits_per_sample=bits_per_sample,
    )


def encode_flac_to_wav(editable: Path, record: SoundRecord) -> bytes:
    """Encode editable FLAC as PCM WAV using the archived member's profile."""
    sample_rate = record.sample_rate
    channels = record.channels
    bits_per_sample = record.bits_per_sample
    try:
        with soundfile.SoundFile(editable) as source:
            if source.format != "FLAC":
                msg = f"Editable sound must be FLAC: {editable}"
                raise ValueError(msg)
            if source.samplerate != sample_rate or source.channels != channels:
                msg = (
                    f"Editable sound must remain {sample_rate} Hz and "
                    f"{channels} channel(s): {editable}"
                )
                raise ValueError(msg)
            samples = source.buffer_read(source.frames, dtype="int16")
        output = io.BytesIO()
        subtype = "PCM_U8" if bits_per_sample == EIGHT_BIT_PCM else "PCM_16"
        with soundfile.SoundFile(
            output,
            mode="w",
            samplerate=sample_rate,
            channels=channels,
            format="WAV",
            subtype=subtype,
        ) as destination:
            destination.buffer_write(bytes(samples), dtype="int16")
    except soundfile.SoundFileError as error:
        msg = f"Cannot encode editable sound {editable}: {error}"
        raise ValueError(msg) from error
    return output.getvalue()


def write_sound_manifest(
    path: Path,
    archive: str,
    sounds: list[SoundRecord],
) -> None:
    """Atomically write one sound manifest."""
    document = SoundManifest(
        schema_version=SOUND_SCHEMA_VERSION,
        archive=archive,
        sounds=sounds,
    )
    serialized = {
        "schemaVersion": document.schema_version,
        "archive": document.archive,
        "sounds": [
            {
                "name": record.name,
                "editable": record.editable,
                "sampleRate": record.sample_rate,
                "channels": record.channels,
                "bitsPerSample": record.bits_per_sample,
            }
            for record in document.sounds
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(serialized, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _member_path(name: str) -> Path:
    candidate = PurePosixPath(name.replace("\\", "/"))
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        msg = f"Unsafe archive member path: {name}"
        raise ValueError(msg)
    return Path(*candidate.parts)


def extract_sound_archive(
    archive_path: Path,
    game_directory: Path,
    pack_root: Path,
) -> tuple[Path, int] | None:
    """Extract WAV members as editable FLAC and retain the source PFS archive."""
    relative_archive = archive_path.relative_to(game_directory)
    archive = PfsArchive(archive_path)
    entries = [
        entry
        for entry in archive.entries
        if Path(entry.name).suffix.lower() == WAV_EXTENSION
    ]
    if not entries:
        return None
    archive_root = pack_root / relative_archive.with_suffix("")
    source = archive_root / "source.pfs"
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive_path, source)
    records: list[SoundRecord] = []
    for entry in entries:
        editable = (
            archive_root
            / "sounds"
            / _member_path(entry.name).with_suffix(FLAC_EXTENSION)
        )
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
    return relative_archive, len(records)


def update_sound_archive(
    archive_path: Path,
    game_directory: Path,
    pack_root: Path,
) -> tuple[list[Path], bool]:
    """Add new FLAC files without overwriting existing editable files."""
    relative_archive = archive_path.relative_to(game_directory)
    archive = PfsArchive(archive_path)
    entries = [
        entry
        for entry in archive.entries
        if Path(entry.name).suffix.lower() == WAV_EXTENSION
    ]
    if not entries:
        return [], False
    archive_root = pack_root / relative_archive.with_suffix("")
    manifest_path = archive_root / SOUND_MANIFEST_NAME
    existing = (
        {
            record.name.casefold(): record
            for record in load_sound_manifest(manifest_path).sounds
        }
        if manifest_path.is_file()
        else {}
    )
    records: list[SoundRecord] = []
    added: list[Path] = []
    for entry in entries:
        record = existing.get(entry.name.casefold())
        editable = (
            archive_root / record.editable
            if record is not None
            else archive_root
            / "sounds"
            / _member_path(entry.name).with_suffix(FLAC_EXTENSION)
        )
        if record is not None and editable.is_file():
            profile = record
        else:
            profile = decode_wav_to_flac(archive.read(entry), editable)
            added.append(editable.relative_to(pack_root))
        records.append(
            SoundRecord(
                name=entry.name,
                editable=editable.relative_to(archive_root).as_posix(),
                sample_rate=profile.sample_rate,
                channels=profile.channels,
                bits_per_sample=profile.bits_per_sample,
            ),
        )
    source = archive_root / "source.pfs"
    source.parent.mkdir(parents=True, exist_ok=True)
    temporary = source.with_name(".source.pfs.tmp")
    shutil.copy2(archive_path, temporary)
    temporary.replace(source)
    write_sound_manifest(manifest_path, relative_archive.as_posix(), records)
    return added, True


def pack_sound_manifest(
    manifest_path: Path,
    packed_root: Path,
    previous_state: ArchivePackState | None = None,
) -> tuple[Path, int, ArchivePackState]:
    """Incrementally rebuild one PFS archive with encoded editable sounds."""
    archive_root = manifest_path.parent
    manifest = load_sound_manifest(manifest_path)
    source_path = archive_root / "source.pfs"
    destination = packed_root / Path(manifest.archive)
    source_sha256 = file_sha256(source_path)
    manifest_sha256 = file_sha256(manifest_path)
    input_hashes: dict[str, str] = {}
    editable_paths: dict[str, Path] = {}
    records_by_name: dict[str, SoundRecord] = {}
    for record in manifest.sounds:
        editable = archive_root / record.editable
        if not editable.is_file():
            msg = f"Editable sound is missing: {editable}"
            raise FileNotFoundError(msg)
        name = record.name
        editable_paths[name] = editable
        records_by_name[name] = record
        input_hashes[name] = file_sha256(editable)

    baseline_state = (
        previous_state
        if previous_state is not None
        and packed_output_matches(destination, previous_state)
        and previous_state.source_sha256 == source_sha256
        and previous_state.manifest_sha256 == manifest_sha256
        else None
    )
    if baseline_state is not None and baseline_state.inputs == input_hashes:
        return destination, 0, baseline_state

    changed_names = (
        {
            name
            for name, digest in input_hashes.items()
            if baseline_state.inputs.get(name) != digest
        }
        if baseline_state is not None
        else set(input_hashes)
    )
    source = PfsArchive(source_path)
    baseline = PfsArchive(destination) if baseline_state is not None else source
    replacements: dict[str, bytes] = {}
    for name in changed_names:
        record = records_by_name[name]
        replacements[name] = encode_flac_to_wav(editable_paths[name], record)
    baseline.rebuild(destination, replacements)
    state = completed_pack_state(
        source_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
        inputs=input_hashes,
        packed_path=destination,
    )
    return destination, len(replacements), state
