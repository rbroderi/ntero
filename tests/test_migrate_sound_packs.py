"""One-off schema-1 sound-pack migration tests."""

import io
import json
import struct
import tempfile
import wave
from pathlib import Path

import soundfile

from migrate_sound_packs import migrate_library
from ntero.sound import (
    SOUND_MANIFEST_NAME,
    SOUND_SCHEMA_VERSION,
    SoundRecord,
    load_sound_manifest,
)


def _wav_bytes(samples: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setframerate(11_025)
        stream.setnchannels(1)
        stream.setsampwidth(1)
        stream.writeframes(samples)
    return output.getvalue()


def test_migrate_library_preserves_edited_wav_audio() -> None:
    """Convert edited schema-1 WAVs and make the migration idempotent."""
    with tempfile.TemporaryDirectory() as temporary:
        library_root = Path(temporary) / "library"
        archive_root = library_root / "sounds" / "voices" / "snd1"
        editable_wav = archive_root / "sounds" / "attack.wav"
        editable_wav.parent.mkdir(parents=True)
        editable_wav.write_bytes(_wav_bytes(bytes([0, 128, 255])))
        manifest_path = archive_root / SOUND_MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "archive": "snd1.pfs",
                    "sounds": [
                        {
                            "name": "attack.wav",
                            "editable": "sounds/attack.wav",
                        },
                    ],
                },
            ),
            encoding="utf-8",
        )

        assert migrate_library(library_root, "voices") == 1
        assert migrate_library(library_root, "voices") == 0

        editable_flac = archive_root / "sounds" / "attack.flac"
        assert not editable_wav.exists()
        with soundfile.SoundFile(editable_flac) as stream:
            assert stream.buffer_read(stream.frames, "int16") == struct.pack(
                "<3h",
                -32_768,
                0,
                32_512,
            )
        manifest = load_sound_manifest(manifest_path)
        assert manifest.schema_version == SOUND_SCHEMA_VERSION
        assert manifest.sounds == [
            SoundRecord(
                name="attack.wav",
                editable="sounds/attack.flac",
                sample_rate=11_025,
                channels=1,
                bits_per_sample=8,
            ),
        ]
