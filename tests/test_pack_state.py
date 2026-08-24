"""Incremental archive pack state tests."""

import json
import tempfile
from pathlib import Path

import pytest

from ntero.pack_state import (
    ArchivePackState,
    completed_pack_state,
    load_pack_state,
    packed_output_matches,
    write_pack_state,
)


def test_pack_state_round_trip_and_output_validation() -> None:
    """Persist archive fingerprints and detect output replacement."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        packed = root / "packed.s3d"
        packed.write_bytes(b"packed")
        state = completed_pack_state(
            source_sha256="source",
            manifest_sha256="manifest",
            inputs={"texture.dds": "input"},
            packed_path=packed,
        )
        path = root / "pack-state.json"

        write_pack_state(path, {"textures/pack-manifest.json": state})

        loaded = load_pack_state(path)
        assert loaded == {"textures/pack-manifest.json": state}
        assert packed_output_matches(packed, loaded["textures/pack-manifest.json"])
        packed.write_bytes(b"changed")
        assert not packed_output_matches(
            packed,
            loaded["textures/pack-manifest.json"],
        )


def test_pack_state_ignores_malformed_records() -> None:
    """Treat invalid state as a cache miss rather than failing packing."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "pack-state.json"
        path.write_text(
            '{"schemaVersion":1,"archives":{"../bad":{"packedSize":1}}}',
            encoding="utf-8",
        )

        assert load_pack_state(path) == {}


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schemaVersion": 0, "archives": {}},
        {"schemaVersion": 1, "archives": []},
        {"schemaVersion": 1, "archives": {"": {}}},
        {"schemaVersion": 1, "archives": {"valid": []}},
        {"schemaVersion": 1, "archives": {"valid": {}}},
        {
            "schemaVersion": 1,
            "archives": {
                "valid/pack-manifest.json": {
                    "sourceSha256": "source",
                    "manifestSha256": "manifest",
                    "packedSize": 1,
                    "packedMtimeNs": 2,
                    "inputs": {"valid": "hash", "invalid": 3},
                },
            },
        },
    ],
)
def test_pack_state_treats_malformed_documents_as_cache_misses(
    document: object,
) -> None:
    """Ignore invalid document shapes and incomplete input hashes."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "pack-state.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        assert load_pack_state(path) == {}


def test_packed_output_requires_previous_state() -> None:
    """Never trust an output that has no successful build record."""
    state: ArchivePackState | None = None
    assert not packed_output_matches(Path("missing.s3d"), state)


def test_packed_output_rejects_missing_file() -> None:
    """Treat a deleted packed output as a cache miss."""
    state = ArchivePackState(
        source_sha256="source",
        manifest_sha256="manifest",
        inputs={},
        packed_size=0,
        packed_mtime_ns=0,
    )

    assert not packed_output_matches(Path("missing.s3d"), state)
