"""Command-line workflow tests."""

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from ntero.cli import DEFAULT_WORKERS
from ntero.cli import GAME_EXECUTABLE_NAME
from ntero.cli import MANIFEST_NAME
from ntero.cli import PACK_DEFAULT_WORKERS
from ntero.cli import UPDATE_LOG_NAME
from ntero.cli import _create_parser
from ntero.cli import _normalize_arguments
from ntero.cli import _play
from ntero.cli import _project_version
from ntero.cli import _update
from ntero.pfs import PfsArchive
from tests.test_pfs import _create_archive

ARGPARSE_ERROR_EXIT = 2
CONFIGURED_WORKERS = 2
EXPECTED_UPDATE_ADDITIONS = 2


def _write_png(path: Path, color: tuple[int, int, int, int]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (1, 1), color).save(path, format="PNG")
    return path.read_bytes()


class CliTests(unittest.TestCase):
    """Verify command parsing and non-destructive pack updates."""

    def test_update_accepts_requested_flag_alias(self) -> None:
        """Treat the requested --update spelling as the update command."""
        options = _create_parser().parse_args(
            _normalize_arguments(
                [
                    "--update",
                    "--library-root",
                    "library",
                    "--texture-pack-name",
                    "edited",
                    "--game-dir",
                    "game",
                ],
            ),
        )

        assert options.command == "update"

    def test_pack_defaults_to_lossy_and_accepts_lossless_mode(self) -> None:
        """Default to BC3 while exposing uncompressed DDS explicitly."""
        parser = _create_parser()
        default = parser.parse_args(
            [
                "pack",
                "--library-root",
                "library",
                "--texture-pack-name",
                "edited",
            ],
        )
        lossless = parser.parse_args(
            [
                "pack",
                "--library-root",
                "library",
                "--texture-pack-name",
                "edited",
                "--lossless",
            ],
        )

        assert not default.lossless
        assert lossless.lossless
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "pack",
                    "--library-root",
                    "library",
                    "--texture-pack-name",
                    "edited",
                    "--lossy",
                ],
            )

    def test_data_commands_accept_benchmark_mode(self) -> None:
        """Expose non-persistent profiling on extract, update, and pack."""
        parser = _create_parser()
        for command in ("extract", "update"):
            options = parser.parse_args(
                [
                    command,
                    "--library-root",
                    "library",
                    "--texture-pack-name",
                    "edited",
                    "--game-dir",
                    "game",
                    "--benchmark",
                ],
            )
            assert options.benchmark

        pack = parser.parse_args(
            [
                "pack",
                "--library-root",
                "library",
                "--texture-pack-name",
                "edited",
                "--benchmark",
            ],
        )
        assert pack.benchmark

        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "play",
                    "--library-root",
                    "library",
                    "--texture-pack-name",
                    "edited",
                    "--game-dir",
                    "game",
                    "--benchmark",
                ],
            )

    def test_commands_accept_explicit_sound_or_texture_packs(self) -> None:
        """Parse either asset pack independently or both together."""
        parser = _create_parser()
        sound_only = parser.parse_args(
            [
                "pack",
                "--library-root",
                "library",
                "--sound-pack-name",
                "voices",
            ],
        )
        combined = parser.parse_args(
            [
                "play",
                "--library-root",
                "library",
                "--texture-pack-name",
                "visuals",
                "--sound-pack-name",
                "voices",
                "--game-dir",
                "game",
            ],
        )

        assert sound_only.texture_pack_name is None
        assert sound_only.sound_pack_name == "voices"
        assert combined.texture_pack_name == "visuals"
        assert combined.sound_pack_name == "voices"

    def test_legacy_pack_name_is_rejected(self) -> None:
        """Require callers to identify texture and sound packs explicitly."""
        with pytest.raises(SystemExit) as exit_info:
            _create_parser().parse_args(
                [
                    "pack",
                    "--library-root",
                    "library",
                    "--pack-name",
                    "legacy",
                ],
            )

        assert exit_info.value.code == ARGPARSE_ERROR_EXIT

    def test_archive_commands_accept_worker_configuration(self) -> None:
        """Use a safe default while allowing explicit archive concurrency."""
        parser = _create_parser()
        default = parser.parse_args(
            [
                "extract",
                "--library-root",
                "library",
                "--texture-pack-name",
                "edited",
                "--game-dir",
                "game",
            ],
        )
        configured = parser.parse_args(
            [
                "pack",
                "--library-root",
                "library",
                "--texture-pack-name",
                "edited",
                "--workers",
                str(CONFIGURED_WORKERS),
            ],
        )
        pack_default = parser.parse_args(
            [
                "pack",
                "--library-root",
                "library",
                "--texture-pack-name",
                "edited",
            ],
        )

        assert default.workers == DEFAULT_WORKERS
        assert pack_default.workers == PACK_DEFAULT_WORKERS
        assert configured.workers == CONFIGURED_WORKERS

    def test_archive_commands_reject_zero_workers(self) -> None:
        """Require at least one archive worker."""
        with pytest.raises(SystemExit) as exit_info:
            _create_parser().parse_args(
                [
                    "pack",
                    "--library-root",
                    "library",
                    "--texture-pack-name",
                    "edited",
                    "--workers",
                    "0",
                ],
            )

        assert exit_info.value.code == ARGPARSE_ERROR_EXIT

    def test_version_reads_project_metadata(self) -> None:
        """Report the version stored in the packaged project metadata."""
        output = io.StringIO()

        with redirect_stdout(output), pytest.raises(SystemExit) as exit_info:
            _create_parser().parse_args(["--version"])

        assert exit_info.value.code == 0
        assert output.getvalue() == f"ntero {_project_version()}\n"

    def test_update_adds_only_missing_textures(self) -> None:
        """Preserve edited PNGs while adding and refreshing current assets."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_directory = root / "game"
            library_root = root / "library"
            archive_root = library_root / "textures" / "edited" / "textures"
            editable_root = archive_root / "textures"
            special_root = archive_root / "special"
            game_directory.mkdir()
            editable_root.mkdir(parents=True)
            special_root.mkdir()

            existing_png = editable_root / "existing.png"
            existing_payload = _write_png(existing_png, (10, 20, 30, 40))
            special_file = special_root / "protected.tga"
            special_file.write_bytes(b"old-special")
            source_archive = (
                library_root / "textures" / "edited" / "textures" / "source.s3d"
            )
            source_archive.write_bytes(
                _create_archive(
                    {"existing.dds": b"old", "protected.tga": b"old-special"},
                ),
            )
            manifest_path = source_archive.parent / MANIFEST_NAME
            manifest_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "archive": "textures.s3d",
                        "textures": [
                            {
                                "name": "existing.dds",
                                "editable": "textures/existing.png",
                                "special": False,
                            },
                            {
                                "name": "protected.tga",
                                "editable": "special/protected.tga",
                                "special": True,
                            },
                        ],
                    },
                ),
                encoding="utf-8",
            )
            (game_directory / "textures.s3d").write_bytes(
                _create_archive(
                    {
                        "existing.dds": b"current-existing",
                        "new.dds": b"current-new",
                        "protected.tga": b"current-special",
                    },
                ),
            )

            def fake_decode(_source: Path, destination: Path) -> None:
                _write_png(destination, (50, 60, 70, 80))

            options = argparse.Namespace(
                game_dir=game_directory,
                library_root=library_root,
                texture_pack_name="edited",
                sound_pack_name=None,
                workers=1,
            )
            output = io.StringIO()
            with (
                patch(
                    "ntero.cli.decode_to_png",
                    side_effect=fake_decode,
                ) as decode,
                redirect_stdout(output),
            ):
                _update(options)

                new_png = editable_root / "new.png"
                new_payload = _write_png(new_png, (90, 100, 110, 120))
                _update(options)
                (game_directory / "textures.s3d").write_bytes(
                    _create_archive(
                        {
                            "existing.dds": b"current-existing",
                            "new.dds": b"current-new",
                            "later.dds": b"current-later",
                            "protected.tga": b"current-special",
                        },
                    ),
                )
                _update(options)

            assert existing_png.read_bytes() == existing_payload
            assert new_png.read_bytes() == new_payload
            assert special_file.read_bytes() == b"current-special"
            assert decode.call_count == EXPECTED_UPDATE_ADDITIONS
            console_output = output.getvalue()
            assert console_output.count("Added file: textures/textures/new.png") == 1
            assert console_output.count("Added file: textures/textures/later.png") == 1
            update_log = (
                library_root / "textures" / "edited" / UPDATE_LOG_NAME
            ).read_text(
                encoding="utf-8",
            )
            assert update_log.count("textures/textures/new.png") == 1
            assert update_log.count("textures/textures/later.png") == 1
            assert update_log.count("[") == EXPECTED_UPDATE_ADDITIONS
            updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert {record["name"] for record in updated_manifest["textures"]} == {
                "existing.dds",
                "later.dds",
                "new.dds",
                "protected.tga",
            }
            refreshed = PfsArchive(source_archive)
            assert refreshed.read("existing.dds") == b"current-existing"
            assert refreshed.read("later.dds") == b"current-later"
            assert refreshed.read("new.dds") == b"current-new"
            assert refreshed.read("protected.tga") == b"current-special"

    def test_play_replaces_overlay_after_successful_build(self) -> None:
        """Deploy a complete overlay and replace packed archives with hardlinks."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_directory = root / "game"
            library_root = root / "library"
            packed_root = library_root / "textures" / "edited" / "packed"
            overlay = library_root / "overlay"
            game_directory.mkdir()
            packed_root.mkdir(parents=True)
            overlay.mkdir(parents=True)
            (game_directory / GAME_EXECUTABLE_NAME).write_bytes(b"game")
            (game_directory / "textures.s3d").write_bytes(b"original")
            (packed_root / "textures.s3d").write_bytes(b"packed")
            (overlay / "stale.txt").write_text("stale", encoding="utf-8")

            _play(
                argparse.Namespace(
                    game_dir=game_directory,
                    library_root=library_root,
                    texture_pack_name="edited",
                    sound_pack_name=None,
                    no_launch=True,
                ),
            )

            assert (overlay / GAME_EXECUTABLE_NAME).read_bytes() == b"game"
            assert (overlay / "textures.s3d").read_bytes() == b"packed"
            assert not (overlay / "stale.txt").exists()

    def test_play_preserves_stale_cleanup_when_hardlink_fails(self) -> None:
        """Do not clean stale paths when creating a required link fails."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game_directory = root / "game"
            library_root = root / "library"
            packed_root = library_root / "textures" / "edited" / "packed"
            overlay = library_root / "overlay"
            game_directory.mkdir()
            packed_root.mkdir(parents=True)
            overlay.mkdir(parents=True)
            (game_directory / GAME_EXECUTABLE_NAME).write_bytes(b"game")
            (packed_root / "textures.s3d").write_bytes(b"packed")
            marker = overlay / "working.txt"
            marker.write_text("keep", encoding="utf-8")
            options = argparse.Namespace(
                game_dir=game_directory,
                library_root=library_root,
                texture_pack_name="edited",
                sound_pack_name=None,
                no_launch=True,
            )

            with (
                patch("ntero.cli.os.link", side_effect=OSError("link failed")),
                pytest.raises(OSError, match="link failed"),
            ):
                _play(options)

            assert marker.read_text(encoding="utf-8") == "keep"


if __name__ == "__main__":
    unittest.main()
