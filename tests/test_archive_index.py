"""Archive manifest index contract tests."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from ntero.archive_index import ARCHIVE_INDEX_NAME
from ntero.archive_index import ArchiveIndexError
from ntero.archive_index import load_manifest_paths
from ntero.archive_index import write_archive_index
from ntero.manifest import MANIFEST_NAME


def _manifest(root: Path, relative: str) -> Path:
    path = root / relative / MANIFEST_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def test_legacy_discovery_writes_and_reuses_archive_index() -> None:
    """Discover an old pack once and use its atomic index thereafter."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first = _manifest(root, "first")
        second = _manifest(root, "zones/second")
        _manifest(root, "packed/ignored")

        assert load_manifest_paths(root, MANIFEST_NAME) == [first, second]
        document = json.loads((root / ARCHIVE_INDEX_NAME).read_text(encoding="utf-8"))
        assert document["manifests"] == [
            "first/pack-manifest.json",
            "zones/second/pack-manifest.json",
        ]

        with patch("ntero.archive_index.discover_manifest_paths") as discover:
            assert load_manifest_paths(root, MANIFEST_NAME) == [first, second]
        discover.assert_not_called()


def test_read_only_legacy_discovery_does_not_write_index() -> None:
    """Allow benchmarks to discover old packs without persistent changes."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        manifest = _manifest(root, "first")

        assert load_manifest_paths(
            root,
            MANIFEST_NAME,
            persist_discovery=False,
        ) == [manifest]
        assert not (root / ARCHIVE_INDEX_NAME).exists()


@pytest.mark.parametrize("value", ["../outside/pack-manifest.json", "wrong.json"])
def test_archive_index_rejects_unsafe_manifest_paths(value: str) -> None:
    """Reject indexed paths that escape the pack or target another file type."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / ARCHIVE_INDEX_NAME).write_text(
            json.dumps({"schemaVersion": 1, "manifests": [value]}),
            encoding="utf-8",
        )

        with pytest.raises(ArchiveIndexError, match="unsafe"):
            load_manifest_paths(root, MANIFEST_NAME)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "root must be an object"),
        ({"schemaVersion": 0, "manifests": []}, "schemaVersion must be 1"),
        ({"schemaVersion": 1, "manifests": {}}, "must be an array"),
        ({"schemaVersion": 1, "manifests": [""]}, "non-empty strings"),
        (
            {
                "schemaVersion": 1,
                "manifests": ["first/pack-manifest.json"] * 2,
            },
            "duplicate manifest paths",
        ),
        (
            {
                "schemaVersion": 1,
                "manifests": ["missing/pack-manifest.json"],
            },
            "Indexed manifest is missing",
        ),
    ],
)
def test_archive_index_rejects_malformed_documents(
    document: object,
    message: str,
) -> None:
    """Reject malformed or stale indexes with a specific contract error."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / ARCHIVE_INDEX_NAME).write_text(
            json.dumps(document),
            encoding="utf-8",
        )

        with pytest.raises(ArchiveIndexError, match=message):
            load_manifest_paths(root, MANIFEST_NAME)


def test_write_archive_index_replaces_stale_entries() -> None:
    """Commit exactly the current manifest set in deterministic order."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        write_archive_index(root, [Path("old") / MANIFEST_NAME], MANIFEST_NAME)
        write_archive_index(
            root,
            [Path("z") / MANIFEST_NAME, Path("a") / MANIFEST_NAME],
            MANIFEST_NAME,
        )

        document = json.loads((root / ARCHIVE_INDEX_NAME).read_text(encoding="utf-8"))
        assert document["manifests"] == [
            "a/pack-manifest.json",
            "z/pack-manifest.json",
        ]
