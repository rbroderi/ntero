"""Sampled benchmark workflow tests."""

import argparse
import io
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from ntero.benchmark import PACK_REPETITIONS
from ntero.benchmark import run_benchmark
from ntero.manifest import MANIFEST_NAME
from ntero.manifest import TextureRecord
from ntero.manifest import write_manifest
from ntero.sound import SOUND_MANIFEST_NAME
from ntero.sound import SoundRecord
from ntero.sound import decode_wav_to_flac
from ntero.sound import write_sound_manifest
from tests.test_pfs import _create_archive


def _options(root: Path, command: str) -> argparse.Namespace:
    return argparse.Namespace(
        benchmark=True,
        command=command,
        game_dir=root / "game",
        library_root=root / "library",
        texture_pack_name="edited",
        sound_pack_name=None,
        lossless=False,
        workers=1,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _wav_bytes(samples: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setframerate(11_025)
        stream.setnchannels(1)
        stream.setsampwidth(1)
        stream.writeframes(samples)
    return output.getvalue()


def test_extract_benchmark_decodes_sample_without_persistent_writes() -> None:
    """Decode sampled archive members only into temporary storage."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "extract")
        options.game_dir.mkdir()
        archive = options.game_dir / "textures.s3d"
        archive.write_bytes(_create_archive({"texture.dds": b"packed"}))
        before = _snapshot(root)

        def fake_decode(_source: Path, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (1, 1), (10, 20, 30, 40)).save(destination)

        with patch("ntero.benchmark.decode_to_png", side_effect=fake_decode) as decode:
            run_benchmark(options)

        decode.assert_called_once()
        assert _snapshot(root) == before


def test_pack_benchmark_encodes_and_rebuilds_without_persistent_writes() -> None:
    """Encode sampled PNGs and rebuild an archive only in temporary storage."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "pack")
        archive_root = options.library_root / "textures" / "edited" / "textures"
        archive_root.mkdir(parents=True)
        (archive_root / "source.s3d").write_bytes(
            _create_archive({"texture.dds": b"original"}),
        )
        editable = archive_root / "textures" / "texture.png"
        editable.parent.mkdir()
        Image.new("RGBA", (1, 1), (10, 20, 30, 40)).save(editable)
        write_manifest(
            archive_root / MANIFEST_NAME,
            "textures.s3d",
            [
                TextureRecord(
                    name="texture.dds",
                    editable="textures/texture.png",
                    special=False,
                    alpha="graded",
                ),
            ],
        )
        before = _snapshot(root)

        def fake_encode(
            _source: Path,
            _name: str,
            *,
            lossy: bool,
            source_dds: bytes | None = None,
            expected_alpha: str | None = None,
        ) -> bytes:
            assert lossy
            assert source_dds is None
            assert expected_alpha == "graded"
            return b"encoded"

        with patch(
            "ntero.benchmark.encode_png_bytes",
            side_effect=fake_encode,
        ) as encode:
            run_benchmark(options)

        assert encode.call_count == PACK_REPETITIONS
        assert _snapshot(root) == before


def test_update_benchmark_processes_missing_member_without_persistent_writes() -> None:
    """Exercise sampled update decoding and metadata work in temporary storage."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "update")
        options.game_dir.mkdir()
        (options.game_dir / "textures.s3d").write_bytes(
            _create_archive({"new.dds": b"new-packed"}),
        )
        pack_root = options.library_root / "textures" / "edited"
        pack_root.mkdir(parents=True)
        before = _snapshot(root)

        def fake_decode(_source: Path, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (1, 1), (10, 20, 30, 40)).save(destination)

        with patch("ntero.benchmark.decode_to_png", side_effect=fake_decode) as decode:
            run_benchmark(options)

        decode.assert_called_once()
        assert _snapshot(root) == before


def test_sound_benchmarks_extract_and_pack_without_persistent_writes() -> None:
    """Read and rebuild sampled WAV members only under temporary storage."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "extract")
        options.texture_pack_name = None
        options.sound_pack_name = "voices"
        options.game_dir.mkdir()
        original = _wav_bytes(bytes([0, 64, 128, 255]))
        (options.game_dir / "snd1.pfs").write_bytes(
            _create_archive({"attack.wav": original}),
        )
        before_extract = _snapshot(root)

        run_benchmark(options)

        assert _snapshot(root) == before_extract

        archive_root = options.library_root / "sounds" / "voices" / "snd1"
        archive_root.mkdir(parents=True)
        (archive_root / "source.pfs").write_bytes(
            _create_archive({"attack.wav": original}),
        )
        editable = archive_root / "sounds" / "attack.flac"
        profile = decode_wav_to_flac(original, editable)
        record = SoundRecord(
            name="attack.wav",
            editable="sounds/attack.flac",
            sample_rate=profile.sample_rate,
            channels=profile.channels,
            bits_per_sample=profile.bits_per_sample,
        )
        write_sound_manifest(
            archive_root / SOUND_MANIFEST_NAME,
            "snd1.pfs",
            [record],
        )
        options.command = "pack"
        before_pack = _snapshot(root)

        run_benchmark(options)

        assert _snapshot(root) == before_pack
