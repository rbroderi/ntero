"""Texture-pack manifest contract tests."""

import json
import tempfile
from pathlib import Path

import pytest

from ntero.manifest import ManifestError
from ntero.manifest import PackManifest
from ntero.manifest import TextureRecord
from ntero.manifest import load_manifest
from ntero.manifest import write_manifest


def test_manifest_round_trip() -> None:
    """Preserve a valid typed manifest through disk serialization."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "pack-manifest.json"
        textures: list[TextureRecord] = [
            TextureRecord(
                name="armor/chest.dds",
                editable="textures/armor/chest.png",
                special=False,
                alpha="graded",
            ),
        ]

        write_manifest(path, "characters/armor.s3d", textures)

        assert load_manifest(path) == PackManifest(
            schema_version=1,
            archive="characters/armor.s3d",
            textures=textures,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("archive", "../outside.s3d"), ("editable", "textures/../../outside.png")],
)
def test_manifest_rejects_path_traversal(field: str, value: str) -> None:
    """Reject manifest paths that escape their assigned pack roots."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "pack-manifest.json"
        archive = value if field == "archive" else "textures.s3d"
        editable = value if field == "editable" else "textures/texture.png"
        manifest = {
            "schemaVersion": 1,
            "archive": archive,
            "textures": [
                {
                    "name": "texture.dds",
                    "editable": editable,
                    "special": False,
                },
            ],
        }
        path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(ManifestError, match="safe relative path"):
            load_manifest(path)


def test_manifest_rejects_unsupported_schema() -> None:
    """Fail clearly instead of interpreting an unknown manifest version."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "pack-manifest.json"
        path.write_text(
            json.dumps({"schemaVersion": 2, "archive": "a.s3d", "textures": []}),
            encoding="utf-8",
        )

        with pytest.raises(ManifestError, match="schemaVersion must be 1"):
            load_manifest(path)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "root must be an object"),
        ({"schemaVersion": 1, "archive": 3, "textures": []}, "archive must"),
        (
            {"schemaVersion": 1, "archive": "a.s3d", "textures": {}},
            "textures must be an array",
        ),
        (
            {"schemaVersion": 1, "archive": "a.s3d", "textures": [1]},
            "textures\\[0\\] must be an object",
        ),
        (
            {
                "schemaVersion": 1,
                "archive": "a.s3d",
                "textures": [{"name": "", "editable": "a.png", "special": False}],
            },
            "name must be a non-empty string",
        ),
        (
            {
                "schemaVersion": 1,
                "archive": "a.s3d",
                "textures": [{"name": "a.dds", "editable": "a.png", "special": 1}],
            },
            "special must be a boolean",
        ),
        (
            {
                "schemaVersion": 1,
                "archive": "a.s3d",
                "textures": [
                    {
                        "name": "a.dds",
                        "editable": "a.png",
                        "special": False,
                        "alpha": "invalid",
                    },
                ],
            },
            "alpha is invalid",
        ),
    ],
)
def test_manifest_rejects_malformed_values(value: object, message: str) -> None:
    """Reject every malformed manifest shape at its contract boundary."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "pack-manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")

        with pytest.raises(ManifestError, match=message):
            load_manifest(path)
