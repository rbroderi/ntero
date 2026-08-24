"""Sound-pack extraction, update, packing, and overlay tests."""

import argparse
import io
import json
import struct
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

import pytest
import soundfile

from ntero.archive_index import load_manifest_paths
from ntero.cli import (
    GAME_EXECUTABLE_NAME,
    _extract_sounds,
    _pack_sounds,
    _play,
    _update_sounds,
)
from ntero.pack_state import PACK_STATE_NAME, ArchivePackState, load_pack_state
from ntero.pfs import PfsArchive
from ntero.sound import (
    SOUND_MANIFEST_NAME,
    SoundManifestError,
    SoundRecord,
    _member_path,
    decode_wav_to_flac,
    encode_flac_to_wav,
    extract_sound_archive,
    load_sound_manifest,
    pack_sound_manifest,
    update_sound_archive,
    write_sound_manifest,
)
from tests.test_pfs import _create_archive

EDITED_FRAME_COUNT = 3
NEW_SOUND_SAMPLE_RATE = 22_050
MALFORMED_SOUND_DOCUMENTS: list[tuple[object, str]] = [
    ([], "root must be an object"),
    ({"schemaVersion": 2, "archive": "snd.pfs", "sounds": {}}, "array"),
    (
        {"schemaVersion": 2, "archive": "snd.pfs", "sounds": [[]]},
        "sounds\\[0\\] must be an object",
    ),
    (
        {
            "schemaVersion": 2,
            "archive": "snd.pfs",
            "sounds": [{"bitsPerSample": 24}],
        },
        "bitsPerSample must be 8 or 16",
    ),
    (
        {
            "schemaVersion": 2,
            "archive": "snd.pfs",
            "sounds": [
                {
                    "name": "",
                    "editable": "sounds/a.flac",
                    "sampleRate": 1,
                    "channels": 1,
                    "bitsPerSample": 8,
                },
            ],
        },
        "must be a non-empty string",
    ),
    (
        {
            "schemaVersion": 2,
            "archive": "snd.pfs",
            "sounds": [
                {
                    "name": "../a.wav",
                    "editable": "sounds/a.flac",
                    "sampleRate": 1,
                    "channels": 1,
                    "bitsPerSample": 8,
                },
            ],
        },
        "must be a safe relative path",
    ),
    (
        {
            "schemaVersion": 2,
            "archive": "snd.pfs",
            "sounds": [
                {
                    "name": "a.wav",
                    "editable": "sounds/a.flac",
                    "sampleRate": 0,
                    "channels": 1,
                    "bitsPerSample": 8,
                },
            ],
        },
        "must be a positive integer",
    ),
]


def _options(root: Path, command: str) -> argparse.Namespace:
    return argparse.Namespace(
        command=command,
        game_dir=root / "game",
        library_root=root / "library",
        texture_pack_name=None,
        sound_pack_name="voices",
        lossless=False,
        no_launch=True,
        workers=1,
    )


def _wav_bytes(
    samples: bytes,
    *,
    sample_rate: int = 11_025,
    channels: int = 1,
    sample_width: int = 1,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setframerate(sample_rate)
        stream.setnchannels(channels)
        stream.setsampwidth(sample_width)
        stream.writeframes(samples)
    return output.getvalue()


def _write_flac(path: Path, samples: tuple[int, ...], sample_rate: int) -> None:
    with soundfile.SoundFile(
        path,
        mode="w",
        samplerate=sample_rate,
        channels=1,
        format="FLAC",
        subtype="PCM_16",
    ) as stream:
        stream.buffer_write(struct.pack(f"<{len(samples)}h", *samples), "int16")


def test_sound_extract_update_and_pack_preserve_edits() -> None:  # noqa: PLR0915
    """Extract WAVs, preserve edits during update, and rebuild the PFS."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "extract")
        options.game_dir.mkdir()
        archive_path = options.game_dir / "snd1.pfs"
        archive_path.write_bytes(
            _create_archive(
                {
                    "attack.wav": _wav_bytes(bytes([0, 64, 128, 255])),
                    "music.txt": b"metadata",
                },
            ),
        )

        _extract_sounds(options)

        pack_root = options.library_root / "sounds" / "voices"
        archive_root = pack_root / "snd1"
        editable = archive_root / "sounds" / "attack.flac"
        assert soundfile.info(editable).format == "FLAC"
        manifest = load_sound_manifest(archive_root / SOUND_MANIFEST_NAME)
        assert manifest.archive == "snd1.pfs"
        assert manifest.sounds == [
            SoundRecord(
                name="attack.wav",
                editable="sounds/attack.flac",
                sample_rate=11_025,
                channels=1,
                bits_per_sample=8,
            ),
        ]
        assert load_manifest_paths(pack_root, SOUND_MANIFEST_NAME) == [
            archive_root / SOUND_MANIFEST_NAME,
        ]
        _write_flac(editable, (-32_768, 0, 32_512), 11_025)
        archive_path.write_bytes(
            _create_archive(
                {
                    "attack.wav": _wav_bytes(bytes([1, 2, 3])),
                    "new.wav": _wav_bytes(
                        struct.pack("<3h", -1000, 0, 1000),
                        sample_rate=NEW_SOUND_SAMPLE_RATE,
                        sample_width=2,
                    ),
                    "music.txt": b"current-metadata",
                },
            ),
        )

        options.command = "update"
        _update_sounds(options)

        assert soundfile.info(editable).frames == EDITED_FRAME_COUNT
        assert (
            soundfile.info(archive_root / "sounds" / "new.flac").samplerate
            == NEW_SOUND_SAMPLE_RATE
        )

        options.command = "pack"
        with patch("ntero.archive_index.discover_manifest_paths") as discover:
            _pack_sounds(options)
        discover.assert_not_called()

        packed = PfsArchive(pack_root / "packed" / "snd1.pfs")
        with soundfile.SoundFile(io.BytesIO(packed.read("attack.wav"))) as attack:
            assert (
                attack.format,
                attack.subtype,
                attack.samplerate,
                attack.channels,
            ) == (
                "WAV",
                "PCM_U8",
                11_025,
                1,
            )
            assert attack.buffer_read(attack.frames, "int16") == struct.pack(
                "<3h",
                -32_768,
                0,
                32_512,
            )
        with soundfile.SoundFile(io.BytesIO(packed.read("new.wav"))) as new_sound:
            assert (new_sound.subtype, new_sound.samplerate) == (
                "PCM_16",
                NEW_SOUND_SAMPLE_RATE,
            )
        assert packed.read("music.txt") == b"current-metadata"

        rebuilds: list[tuple[Path, set[str]]] = []
        rebuild = PfsArchive.rebuild

        def track_rebuild(
            archive: PfsArchive,
            destination: Path,
            replacements: dict[str, bytes],
        ) -> None:
            rebuilds.append((archive.path, set(replacements)))
            rebuild(archive, destination, replacements)

        with patch(
            "ntero.sound.PfsArchive.rebuild",
            autospec=True,
            side_effect=track_rebuild,
        ):
            _pack_sounds(options)
            _write_flac(editable, (-16_384, 16_384), 11_025)
            _pack_sounds(options)
            packed_path = pack_root / "packed" / "snd1.pfs"
            packed_path.write_bytes(b"externally replaced")
            _pack_sounds(options)
            (archive_root / "source.pfs").write_bytes(
                _create_archive(
                    {
                        "attack.wav": _wav_bytes(bytes([4, 5, 6])),
                        "new.wav": _wav_bytes(
                            struct.pack("<2h", -2000, 2000),
                            sample_rate=NEW_SOUND_SAMPLE_RATE,
                            sample_width=2,
                        ),
                        "music.txt": b"latest-metadata",
                    },
                ),
            )
            _pack_sounds(options)

        assert rebuilds == [
            (pack_root / "packed" / "snd1.pfs", {"attack.wav"}),
            (
                archive_root / "source.pfs",
                {"attack.wav", "new.wav"},
            ),
            (
                archive_root / "source.pfs",
                {"attack.wav", "new.wav"},
            ),
        ]
        repacked = PfsArchive(pack_root / "packed" / "snd1.pfs")
        with soundfile.SoundFile(io.BytesIO(repacked.read("attack.wav"))) as attack:
            assert attack.buffer_read(attack.frames, "int16") == struct.pack(
                "<2h",
                -16_384,
                16_384,
            )
        with soundfile.SoundFile(io.BytesIO(repacked.read("new.wav"))) as new_sound:
            assert new_sound.buffer_read(new_sound.frames, "int16") == struct.pack(
                "<3h",
                -1000,
                0,
                1000,
            )
        assert repacked.read("music.txt") == b"latest-metadata"


def test_encode_rejects_changed_flac_profile() -> None:
    """Reject sample-rate changes rather than writing an incompatible WAV."""
    with tempfile.TemporaryDirectory() as temporary:
        editable = Path(temporary) / "attack.flac"
        _write_flac(editable, (-1000, 1000), NEW_SOUND_SAMPLE_RATE)
        record = SoundRecord(
            name="attack.wav",
            editable="sounds/attack.flac",
            sample_rate=11_025,
            channels=1,
            bits_per_sample=8,
        )

        with pytest.raises(ValueError, match="must remain 11025 Hz"):
            encode_flac_to_wav(editable, record)


def test_runtime_rejects_schema_one_sound_manifest() -> None:
    """Require the one-off migration before normal commands use an old pack."""
    with tempfile.TemporaryDirectory() as temporary:
        manifest = Path(temporary) / SOUND_MANIFEST_NAME
        manifest.write_text(
            '{"schemaVersion": 1, "archive": "snd1.pfs", "sounds": []}',
            encoding="utf-8",
        )

        with pytest.raises(SoundManifestError, match="schemaVersion must be 2"):
            load_sound_manifest(manifest)


@pytest.mark.parametrize(
    ("document", "message"),
    MALFORMED_SOUND_DOCUMENTS,
)
def test_sound_manifest_rejects_malformed_documents(
    document: object,
    message: str,
) -> None:
    """Reject invalid manifest shapes, paths, and audio profiles."""
    with tempfile.TemporaryDirectory() as temporary:
        manifest = Path(temporary) / SOUND_MANIFEST_NAME
        manifest.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(SoundManifestError, match=message):
            load_sound_manifest(manifest)


def test_sound_codecs_reject_unsupported_or_unreadable_inputs() -> None:
    """Report unsupported containers and unreadable audio consistently."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        flac = root / "source.flac"
        _write_flac(flac, (-1000, 1000), 11_025)

        with pytest.raises(ValueError, match="Unsupported archived WAV format"):
            decode_wav_to_flac(flac.read_bytes(), root / "editable.flac")
        with pytest.raises(ValueError, match="Cannot decode PCM WAV"):
            decode_wav_to_flac(b"not audio", root / "editable.flac")

        wav = root / "editable.wav"
        wav.write_bytes(_wav_bytes(bytes([0, 255])))
        record = SoundRecord("a.wav", "editable.wav", 11_025, 1, 8)
        with pytest.raises(ValueError, match="Editable sound must be FLAC"):
            encode_flac_to_wav(wav, record)
        with pytest.raises(ValueError, match="Cannot encode editable sound"):
            encode_flac_to_wav(root / "missing.flac", record)


@pytest.mark.parametrize("name", ["../escape.wav", "/absolute.wav"])
def test_sound_member_paths_must_be_relative(name: str) -> None:
    """Reject archive members that could escape the extraction root."""
    with pytest.raises(ValueError, match="Unsafe archive member path"):
        _member_path(name)


def test_sound_archive_operations_skip_archives_without_wavs() -> None:
    """Leave archives with no sound members untouched during extraction and update."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        game = root / "game"
        game.mkdir()
        archive = game / "data.pfs"
        archive.write_bytes(_create_archive({"notes.txt": b"metadata"}))
        pack_root = root / "pack"

        assert extract_sound_archive(archive, game, pack_root) is None
        assert update_sound_archive(archive, game, pack_root) == ([], False)


def test_pack_sound_manifest_requires_every_editable() -> None:
    """Fail before rebuilding when a manifest references a missing FLAC."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "source.pfs").write_bytes(b"source")
        manifest = root / SOUND_MANIFEST_NAME
        write_sound_manifest(
            manifest,
            "snd.pfs",
            [SoundRecord("a.wav", "sounds/a.flac", 11_025, 1, 8)],
        )

        with pytest.raises(FileNotFoundError, match="Editable sound is missing"):
            pack_sound_manifest(manifest, root / "packed")


def test_sound_commands_require_an_extracted_pack() -> None:
    """Reject update and pack before a sound pack has been extracted."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "update")
        options.game_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="Pack does not exist"):
            _update_sounds(options)

        options.command = "pack"
        with pytest.raises(FileNotFoundError, match="No extracted sound manifests"):
            _pack_sounds(options)


def test_pack_sounds_checkpoints_state_before_propagating_failure() -> None:
    """Keep completed sound archive state when a later archive fails."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "pack")
        pack_root = options.library_root / "sounds" / "voices"
        for name in ("first", "second"):
            manifest = pack_root / name / SOUND_MANIFEST_NAME
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}", encoding="utf-8")
        completed_state = ArchivePackState(
            source_sha256="source",
            manifest_sha256="manifest",
            inputs={},
            packed_size=1,
            packed_mtime_ns=1,
        )

        def process(
            manifest: Path,
            _packed_root: Path,
            _previous_state: ArchivePackState | None,
        ) -> tuple[Path, int, ArchivePackState]:
            if manifest.parent.name == "second":
                msg = "encode failed"
                raise ValueError(msg)
            return Path("first.pfs"), 1, completed_state

        with (
            patch("ntero.cli.pack_sound_manifest", side_effect=process),
            pytest.raises(ValueError, match="encode failed"),
        ):
            _pack_sounds(options)

        assert load_pack_state(pack_root / PACK_STATE_NAME) == {
            "first/sound-manifest.json": completed_state,
        }


def test_play_combines_texture_and_sound_packs() -> None:
    """Overlay both selected packed roots over the game installation."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        game_root = root / "game"
        library_root = root / "library"
        texture_packed = library_root / "textures" / "visuals" / "packed"
        sound_packed = library_root / "sounds" / "voices" / "packed"
        game_root.mkdir()
        texture_packed.mkdir(parents=True)
        sound_packed.mkdir(parents=True)
        (game_root / GAME_EXECUTABLE_NAME).write_bytes(b"game")
        (game_root / "world.s3d").write_bytes(b"original-texture")
        (game_root / "snd1.pfs").write_bytes(b"original-sound")
        texture_archive = texture_packed / "world.s3d"
        sound_archive = sound_packed / "snd1.pfs"
        texture_archive.write_bytes(b"packed-texture")
        sound_archive.write_bytes(b"packed-sound")
        old_overlay = library_root / "overlay" / "visuals+voices"
        old_overlay.mkdir(parents=True)
        (old_overlay / "stale.txt").write_text("stale", encoding="utf-8")
        options = argparse.Namespace(
            game_dir=game_root,
            library_root=library_root,
            texture_pack_name="visuals",
            sound_pack_name="voices",
            no_launch=True,
        )

        _play(options)

        overlay = library_root / "overlay"
        assert (overlay / "world.s3d").samefile(texture_archive)
        assert (overlay / "snd1.pfs").samefile(sound_archive)
        assert not old_overlay.exists()
