"""Command workflow, routing, and failure-path tests."""

import argparse
import os
import runpy
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from PIL import Image

from ntero.alpha import AlphaMismatchError
from ntero.archive_index import load_manifest_paths
from ntero.archive_index import write_archive_index
from ntero.cli import GAME_EXECUTABLE_NAME
from ntero.cli import MANIFEST_NAME
from ntero.cli import _existing_update_record
from ntero.cli import _extract
from ntero.cli import _launch_game
from ntero.cli import _load_existing_records
from ntero.cli import _materialize_texture
from ntero.cli import _missing_update_record
from ntero.cli import _normalize_arguments
from ntero.cli import _pack
from ntero.cli import _play
from ntero.cli import _populate_overlay
from ntero.cli import _project_version
from ntero.cli import _ProjectMetadataError
from ntero.cli import _safe_member_path
from ntero.cli import _safe_pack_root
from ntero.cli import _sync_overlay_links
from ntero.cli import _TextureContext
from ntero.cli import _update
from ntero.cli import _update_archive
from ntero.cli import main
from ntero.decoder import TextureDecodeError
from ntero.encoder import TextureEncodeError
from ntero.manifest import TextureRecord
from ntero.manifest import load_manifest
from ntero.manifest import write_manifest
from ntero.pack_state import PACK_STATE_NAME
from ntero.pack_state import ArchivePackState
from ntero.pack_state import load_pack_state
from ntero.pfs import PfsArchive
from tests.test_pfs import _create_archive

EXPECTED_ENTRYPOINT_EXIT = 7
EXPECTED_EXTENDED_BMP_MIPS = 2
EXPECTED_SELECTIVE_ENCODINGS = 2
WORKER_BARRIER_TIMEOUT = 5
SUBPROCESS_TIMEOUT = 30


def _write_png(path: Path, color: tuple[int, int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (1, 1), color).save(path, format="PNG")


def _options(root: Path, command: str = "extract") -> argparse.Namespace:
    return argparse.Namespace(
        command=command,
        game_dir=root / "game",
        library_root=root / "library",
        texture_pack_name="edited",
        sound_pack_name=None,
        lossless=False,
        no_launch=True,
        workers=2,
    )


@pytest.mark.parametrize(
    ("document", "message"),
    [({}, "project.*table"), ({"project": {}}, "project version")],
)
def test_project_version_rejects_incomplete_metadata(
    document: dict[str, object],
    message: str,
) -> None:
    """Reject project metadata without the required PEP 621 fields."""
    with (
        patch("ntero.cli.tomllib.load", return_value=document),
        pytest.raises(_ProjectMetadataError, match=message),
    ):
        _project_version()


@pytest.mark.parametrize("name", ["", " ", ".", "..", "nested/name"])
def test_safe_pack_root_rejects_invalid_names(name: str) -> None:
    """Require one non-empty directory name for a texture pack."""
    with pytest.raises(ValueError, match="one directory name"):
        _safe_pack_root(Path("library"), name)


@pytest.mark.parametrize("name", ["/absolute.dds", "../outside.dds"])
def test_safe_member_path_rejects_unsafe_names(name: str) -> None:
    """Reject absolute and traversal archive member paths."""
    with pytest.raises(ValueError, match="Unsafe archive member path"):
        _safe_member_path(name)


def test_safe_paths_accept_nested_members() -> None:
    """Resolve safe pack names and nested archive members."""
    pack_root = _safe_pack_root(Path("library"), "edited")
    assert pack_root.name == "edited"
    assert pack_root.parent.name == "textures"
    assert _safe_member_path(r"armor\chest.dds") == Path("armor/chest.dds")


def test_safe_pack_root_migrates_legacy_pack() -> None:
    """Move an existing pack under the library's textures directory."""
    with tempfile.TemporaryDirectory() as temporary:
        library_root = Path(temporary)
        legacy_root = library_root / "edited"
        legacy_root.mkdir()
        marker = legacy_root / "marker.txt"
        marker.write_text("preserved", encoding="utf-8")

        pack_root = _safe_pack_root(library_root, "edited")

        assert pack_root == library_root / "textures" / "edited"
        assert (pack_root / "marker.txt").read_text(encoding="utf-8") == "preserved"
        assert not legacy_root.exists()


def test_extract_decodes_textures_and_preserves_special_files() -> None:
    """Extract editable and special textures while skipping irrelevant archives."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root)
        options.game_dir.mkdir()
        (options.game_dir / "sky.s3d").write_bytes(
            _create_archive({"sky.dds": b"skip"}),
        )
        (options.game_dir / "world.s3d").write_bytes(
            _create_archive({"world.wld": b"world"}),
        )
        (options.game_dir / "textures.s3d").write_bytes(
            _create_archive({"good.dds": b"good", "bad.tga": b"bad"}),
        )

        def fake_decode(source: Path, destination: Path) -> None:
            if source.read_bytes() == b"bad":
                msg = "unsupported"
                raise TextureDecodeError(msg)
            _write_png(destination, (10, 20, 30, 40))

        with patch("ntero.cli.decode_to_png", side_effect=fake_decode):
            _extract(options)

        archive_root = options.library_root / "textures" / "edited" / "textures"
        manifest = load_manifest(archive_root / MANIFEST_NAME)
        with Image.open(archive_root / "textures" / "good.png") as decoded:
            assert decoded.getpixel((0, 0)) == (10, 20, 30, 40)
        assert (archive_root / "special" / "bad.tga").read_bytes() == b"bad"
        assert {record.special for record in manifest.textures} == {False, True}
        assert (
            next(record.alpha for record in manifest.textures if not record.special)
            == "graded"
        )
        assert not (options.library_root / "textures" / "edited" / "sky").exists()
        assert not (options.library_root / "textures" / "edited" / "world").exists()
        assert load_manifest_paths(
            options.library_root / "textures" / "edited",
            MANIFEST_NAME,
        ) == [archive_root / MANIFEST_NAME]


def test_extract_processes_archives_concurrently() -> None:
    """Overlap independent archive jobs when more than one worker is available."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root)
        options.workers = 2
        options.game_dir.mkdir()
        archives = [options.game_dir / "first.s3d", options.game_dir / "second.s3d"]
        for archive in archives:
            archive.touch()
        barrier = threading.Barrier(options.workers)
        progress = MagicMock()
        progress_context = MagicMock()
        progress_context.__enter__.return_value = progress

        def process_archive(
            archive_path: Path,
            *,
            game_directory: Path,
            pack_root: Path,
        ) -> tuple[Path, int]:
            assert pack_root.name == "edited"
            barrier.wait(timeout=WORKER_BARRIER_TIMEOUT)
            return archive_path.relative_to(game_directory), 1

        with (
            patch("ntero.cli._extract_archive", new=process_archive),
            patch("ntero.cli.alive_bar", return_value=progress_context) as alive_bar,
        ):
            _extract(options)

        assert barrier.n_waiting == 0
        alive_bar.assert_called_once_with(
            len(archives),
            title="Extracting S3D archives",
            enrich_print=False,
        )
        assert progress.call_count == len(archives)


def test_materialize_removes_partial_editable_on_failure() -> None:
    """Remove partially written PNG output before preserving the original file."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.s3d"
        source.write_bytes(_create_archive({"bad.dds": b"original"}))
        archive_root = root / "pack"
        context = _TextureContext(
            PfsArchive(source),
            archive_root,
            root / "work",
        )

        def fail_decode(_source: Path, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"partial")
            msg = "bad"
            raise TextureDecodeError(msg)

        with patch("ntero.cli.decode_to_png", side_effect=fail_decode):
            record = _materialize_texture(context, Path("bad.dds"), "bad.dds")

        assert record.special
        assert not (archive_root / "textures" / "bad.png").exists()
        assert not (root / "work" / "bad.dds").exists()


def test_pack_rebuilds_archive_and_skips_special_records() -> None:
    """Encode editable records and preserve protected source bytes."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "pack")
        archive_root = options.library_root / "textures" / "edited" / "textures"
        archive_root.mkdir(parents=True)
        (archive_root / "source.s3d").write_bytes(
            _create_archive({"edit.dds": b"old", "special.tga": b"protected"}),
        )
        editable = archive_root / "textures" / "edit.png"
        _write_png(editable, (10, 20, 30, 40))
        write_manifest(
            archive_root / MANIFEST_NAME,
            "textures.s3d",
            [
                TextureRecord(
                    name="edit.dds",
                    editable="textures/edit.png",
                    special=False,
                ),
                TextureRecord(
                    name="special.tga",
                    editable="special/special.tga",
                    special=True,
                ),
            ],
        )
        nested_packed = (
            options.library_root / "textures" / "edited" / "packed" / "ignored"
        )
        nested_packed.mkdir(parents=True)
        (nested_packed / MANIFEST_NAME).write_text("{}", encoding="utf-8")
        write_archive_index(
            options.library_root / "textures" / "edited",
            [Path("textures") / MANIFEST_NAME],
            MANIFEST_NAME,
        )
        legacy_encoded = archive_root / "encoded"
        legacy_encoded.mkdir()
        (legacy_encoded / "edit.dds").write_bytes(b"legacy")
        legacy_cache = (
            options.library_root / "textures" / "edited" / "texture-hashes.json"
        )
        legacy_cache.write_text("{}", encoding="utf-8")

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
            assert expected_alpha is None
            return b"new"

        rebuilds: list[tuple[Path, set[str]]] = []
        rebuild = PfsArchive.rebuild

        def track_rebuild(
            archive: PfsArchive,
            destination: Path,
            replacements: dict[str, bytes],
        ) -> None:
            rebuilds.append((archive.path, set(replacements)))
            rebuild(archive, destination, replacements)

        with (
            patch(
                "ntero.cli.encode_png_bytes",
                side_effect=fake_encode,
            ) as encode,
            patch("ntero.archive_index.discover_manifest_paths") as discover,
            patch(
                "ntero.cli.PfsArchive.rebuild",
                autospec=True,
                side_effect=track_rebuild,
            ),
        ):
            _pack(options)
            _pack(options)
            _write_png(editable, (50, 60, 70, 80))
            _pack(options)

        discover.assert_not_called()

        packed = PfsArchive(
            options.library_root / "textures" / "edited" / "packed" / "textures.s3d",
        )
        assert packed.read("edit.dds") == b"new"
        assert packed.read("special.tga") == b"protected"
        assert encode.call_count == EXPECTED_SELECTIVE_ENCODINGS
        assert rebuilds == [
            (archive_root / "source.s3d", {"edit.dds"}),
            (
                options.library_root
                / "textures"
                / "edited"
                / "packed"
                / "textures.s3d",
                {"edit.dds"},
            ),
        ]
        assert not legacy_encoded.exists()
        assert not legacy_cache.exists()


def test_lossless_pack_uses_bgra_dds_for_bmp_member_name() -> None:
    """Encode logical BMP archive members as uncompressed DDS when requested."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "pack")
        options.lossless = True
        options.workers = 1
        archive_root = options.library_root / "textures" / "edited" / "freporte"
        archive_root.mkdir(parents=True)
        source_dds = bytearray(128)
        source_dds[:4] = b"DDS "
        source_dds[84:88] = b"DXT1"
        (archive_root / "source.s3d").write_bytes(
            _create_archive({"grubin03.bmp": bytes(source_dds)}),
        )
        editable = archive_root / "textures" / "grubin03.png"
        editable.parent.mkdir(parents=True)
        Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(editable)
        write_manifest(
            archive_root / MANIFEST_NAME,
            "freporte.s3d",
            [
                TextureRecord(
                    name="grubin03.bmp",
                    editable="textures/grubin03.png",
                    special=False,
                ),
            ],
        )

        _pack(options)

        payload = PfsArchive(
            options.library_root / "textures" / "edited" / "packed" / "freporte.s3d",
        ).read("grubin03.bmp")
        assert payload[:4] == b"DDS "
        assert payload[84:88] == b"\0\0\0\0"
        assert struct.unpack_from("<I", payload, 28)[0] == EXPECTED_EXTENDED_BMP_MIPS


def test_pack_checkpoints_state_before_propagating_failure() -> None:
    """Keep completed archive state when a later archive fails."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "pack")
        options.workers = 1
        pack_root = options.library_root / "textures" / "edited"
        for name in ("first", "second"):
            manifest = pack_root / name / MANIFEST_NAME
            manifest.parent.mkdir(parents=True)
            manifest.write_text("{}", encoding="utf-8")
        completed_state = ArchivePackState(
            source_sha256="source",
            manifest_sha256="manifest",
            inputs={},
            packed_size=1,
            packed_mtime_ns=1,
        )

        def process(manifest: Path, *, context: object) -> object:
            del context
            if manifest.parent.name == "second":
                message = "encode failed"
                raise TextureEncodeError(message)
            return (
                Path("first.s3d"),
                1,
                completed_state,
            )

        with (
            patch("ntero.cli._pack_manifest", side_effect=process),
            pytest.raises(TextureEncodeError, match="encode failed"),
        ):
            _pack(options)

        assert load_pack_state(pack_root / PACK_STATE_NAME) == {
            "first/pack-manifest.json": completed_state,
        }


def test_pack_reports_missing_inputs() -> None:
    """Report absent manifests and editable files with their exact paths."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "pack")
        options.library_root.mkdir()
        with pytest.raises(FileNotFoundError, match="No extracted pack manifests"):
            _pack(options)

        archive_root = options.library_root / "textures" / "edited" / "textures"
        archive_root.mkdir(parents=True)
        (archive_root / "source.s3d").write_bytes(_create_archive({"edit.dds": b"old"}))
        write_manifest(
            archive_root / MANIFEST_NAME,
            "textures.s3d",
            [
                TextureRecord(
                    name="edit.dds",
                    editable="textures/missing.png",
                    special=False,
                ),
            ],
        )
        with pytest.raises(FileNotFoundError, match="Editable texture is missing"):
            _pack(options)


def test_pack_rejects_lost_graded_alpha() -> None:
    """Stop before encoding when an edited PNG loses required graded alpha."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "pack")
        archive_root = options.library_root / "textures" / "edited" / "textures"
        archive_root.mkdir(parents=True)
        (archive_root / "source.s3d").write_bytes(
            _create_archive({"edit.dds": b"old"}),
        )
        editable = archive_root / "textures" / "edit.png"
        editable.parent.mkdir()
        Image.new("RGB", (1, 1), (10, 20, 30)).save(editable, format="PNG")
        write_manifest(
            archive_root / MANIFEST_NAME,
            "textures.s3d",
            [
                TextureRecord(
                    name="edit.dds",
                    editable="textures/edit.png",
                    special=False,
                    alpha="graded",
                ),
            ],
        )

        with pytest.raises(AlphaMismatchError, match="graded to none"):
            _pack(options)

        assert not (archive_root / "encoded" / "edit.dds").exists()
        assert not (
            options.library_root / "textures" / "edited" / "packed" / "textures.s3d"
        ).exists()


def test_update_helpers_handle_empty_archives_and_missing_manifests() -> None:
    """Return neutral results when no manifest or texture members exist."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive_path = root / "world.s3d"
        archive_path.write_bytes(_create_archive({"world.wld": b"world"}))

        assert _load_existing_records(root / "missing.json") == {}
        assert _update_archive(archive_path, Path("world.s3d"), root) == (
            [],
            0,
            False,
        )


def test_missing_update_record_recovers_orphan_editable() -> None:
    """Reuse an editable PNG found on disk even without a manifest record."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.s3d"
        source.write_bytes(_create_archive({"texture.dds": b"original"}))
        archive_root = root / "pack"
        editable = archive_root / "textures" / "texture.png"
        _write_png(editable, (10, 20, 30, 40))
        context = _TextureContext(
            PfsArchive(source),
            archive_root,
            root / "work",
        )

        with patch("ntero.cli.decode_to_png") as decode:
            record, added = _missing_update_record(
                context,
                Path("texture.dds"),
                "texture.dds",
            )

        assert not added
        assert record.editable == "textures/texture.png"
        decode.assert_not_called()


def test_existing_update_record_recreates_deleted_editable() -> None:
    """Treat a manifest record with a deleted PNG as missing."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.s3d"
        source.write_bytes(_create_archive({"texture.dds": b"original"}))
        existing = TextureRecord(
            name="texture.dds",
            editable="textures/deleted.png",
            special=False,
            alpha="graded",
        )

        assert _existing_update_record(
            PfsArchive(source),
            root / "pack",
            Path("texture.dds"),
            "texture.dds",
            existing,
        ) == (None, False)


def test_update_rejects_missing_pack_and_skips_sky() -> None:
    """Require an existing pack and ignore the excluded sky archive."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "update")
        options.game_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="Pack does not exist"):
            _update(options)

        (options.library_root / "textures" / "edited").mkdir(parents=True)
        (options.game_dir / "sky.s3d").write_bytes(
            _create_archive({"sky.dds": b"skip"}),
        )
        _update(options)
        assert not (options.library_root / "textures" / "edited" / "sky").exists()


def test_populate_overlay_hardlinks_game_files_and_packed_overrides() -> None:
    """Link all game files while preferring packed S3Ds at matching paths."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        game_root = root / "game"
        packed_root = root / "packed"
        game_root.mkdir()
        packed_root.mkdir()
        overlay = root / "overlay"
        overlay.mkdir()
        game_executable = game_root / GAME_EXECUTABLE_NAME
        game_executable.write_bytes(b"game")
        (game_root / "textures.s3d").write_bytes(b"original")
        packed_archive = packed_root / "textures.s3d"
        packed_archive.write_bytes(b"packed")

        _populate_overlay(game_root, [packed_root], overlay)

        assert (overlay / GAME_EXECUTABLE_NAME).samefile(game_executable)
        assert (overlay / "textures.s3d").samefile(packed_archive)


def test_populate_overlay_requires_game_executable() -> None:
    """Reject an overlay copied from an incomplete game installation."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        game_root = root / "game"
        packed_root = root / "packed"
        game_root.mkdir()
        packed_root.mkdir()
        with pytest.raises(FileNotFoundError, match="The Game executable"):
            _populate_overlay(game_root, [packed_root], root / "overlay")


def test_sync_overlay_links_reuses_links_and_removes_stale_paths() -> None:
    """Reuse valid links while removing files absent from both sources."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        game_root = root / "game"
        packed_root = root / "packed"
        overlay = root / "overlay"
        game_root.mkdir()
        packed_root.mkdir()
        overlay.mkdir()
        game_file = game_root / "game.dat"
        game_file.write_bytes(b"game")
        os.link(game_file, overlay / "game.dat")
        stale = overlay / "stale.s3d"
        stale.write_bytes(b"stale")

        with patch("ntero.cli.os.link") as link:
            _sync_overlay_links(game_root, [packed_root], overlay)

        link.assert_not_called()
        assert not stale.exists()


def test_sync_overlay_links_replaces_directory_with_file() -> None:
    """Replace a stale overlay directory when the desired path is a file."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        game_root = root / "game"
        overlay = root / "overlay"
        game_root.mkdir()
        overlay.mkdir()
        source = game_root / "game.dat"
        source.write_bytes(b"game")
        stale_directory = overlay / "game.dat"
        stale_directory.mkdir()
        (stale_directory / "stale.txt").write_bytes(b"stale")

        _sync_overlay_links(game_root, [], overlay)

        assert (overlay / "game.dat").samefile(source)


def test_play_requires_packed_build_and_launches_game() -> None:
    """Require packed assets and delegate game startup when launch is enabled."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        options = _options(root, "play")
        options.game_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="Pack has not been built"):
            _play(options)

        packed_root = options.library_root / "textures" / "edited" / "packed"
        packed_root.mkdir(parents=True)
        options.no_launch = False
        executable = root / "overlay" / GAME_EXECUTABLE_NAME

        with (
            patch("ntero.cli._build_overlay", return_value=executable),
            patch("ntero.cli._launch_game") as launch,
        ):
            _play(options)
        launch.assert_called_once_with(
            executable,
            options.library_root / "overlay",
        )


def test_launch_game_starts_patchme_process() -> None:
    """Start The Game in patchme mode from the completed overlay."""
    executable = Path(GAME_EXECUTABLE_NAME)
    overlay = Path("overlay")
    with patch("ntero.cli.subprocess.Popen") as create_process:
        _launch_game(executable, overlay)

    create_process.assert_called_once_with(
        [str(executable), "patchme"],
        cwd=overlay,
    )


@pytest.mark.parametrize(
    ("arguments", "target"),
    [
        (
            [
                "extract",
                "--library-root",
                "lib",
                "--texture-pack-name",
                "pack",
                "--game-dir",
                "game",
            ],
            "_extract",
        ),
        (
            [
                "update",
                "--library-root",
                "lib",
                "--texture-pack-name",
                "pack",
                "--game-dir",
                "game",
            ],
            "_update",
        ),
        (
            ["pack", "--library-root", "lib", "--texture-pack-name", "pack"],
            "_pack",
        ),
        (
            [
                "play",
                "--library-root",
                "lib",
                "--texture-pack-name",
                "pack",
                "--game-dir",
                "game",
            ],
            "_play",
        ),
    ],
)
def test_main_routes_commands(arguments: list[str], target: str) -> None:
    """Dispatch every parser command to its workflow."""
    with patch(f"ntero.cli.{target}") as workflow:
        assert main(arguments) == 0
    workflow.assert_called_once()


def test_main_formats_workflow_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """Convert expected workflow failures into argparse process errors."""
    arguments = [
        "pack",
        "--library-root",
        "lib",
        "--texture-pack-name",
        "pack",
    ]
    with (
        patch("ntero.cli._pack", side_effect=OSError("disk full")),
        pytest.raises(SystemExit) as exit_info,
    ):
        main(arguments)

    assert exit_info.value.code == 1
    assert "error: disk full" in capsys.readouterr().err


def test_main_routes_benchmark_without_running_persistent_workflow() -> None:
    """Dispatch benchmark mode before the ordinary command workflow."""
    arguments = [
        "extract",
        "--library-root",
        "lib",
        "--texture-pack-name",
        "pack",
        "--game-dir",
        "game",
        "--benchmark",
    ]
    with (
        patch("ntero.cli.run_benchmark") as benchmark,
        patch("ntero.cli._extract") as extract,
    ):
        assert main(arguments) == 0

    benchmark.assert_called_once()
    extract.assert_not_called()


def test_main_requires_an_explicit_pack(capsys: pytest.CaptureFixture[str]) -> None:
    """Reject commands that select neither a texture nor a sound pack."""
    with pytest.raises(SystemExit) as exit_info:
        main(["pack", "--library-root", "lib"])

    assert exit_info.value.code == 1
    assert "Select --texture-pack-name" in capsys.readouterr().err


def test_package_main_executes_cli_entrypoint() -> None:
    """Delegate package execution to the CLI and preserve its exit status."""
    with (
        patch("multiprocessing.freeze_support") as freeze_support,
        patch("ntero.cli.main", return_value=EXPECTED_ENTRYPOINT_EXIT),
        pytest.raises(SystemExit) as exit_info,
    ):
        runpy.run_module("ntero.__main__", run_name="__main__")

    assert exit_info.value.code == EXPECTED_ENTRYPOINT_EXIT
    freeze_support.assert_called_once_with()


def test_package_runtime_type_checks_resolve_annotations() -> None:
    """Enable runtime checks automatically for every package import."""
    program = """
from beartype.roar import BeartypeCallHintParamViolation

from ntero.cli import _worker_count
from ntero.manifest import TextureRecord

checks = (
    lambda: _worker_count(1),
    lambda: TextureRecord(
        name="a.dds",
        editable="a.png",
        special=False,
        alpha=1,
    ),
)
for check in checks:
    try:
        check()
    except BeartypeCallHintParamViolation:
        continue
    raise AssertionError("Beartype did not enforce a runtime annotation")
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT,
    )

    assert result.returncode == 0, result.stderr
    assert "BeartypeClawDecorWarning" not in result.stderr


def test_normalize_arguments_reads_sys_argv() -> None:
    """Use process arguments when callers do not provide an explicit list."""
    with patch.object(sys, "argv", ["ntero", "--update"]):
        assert _normalize_arguments(None) == ["update"]
