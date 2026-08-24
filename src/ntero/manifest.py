"""Read and write validated texture-pack manifests."""

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from ntero.alpha import AlphaMode

MANIFEST_NAME = "pack-manifest.json"
SCHEMA_VERSION = 1


class ManifestError(ValueError):
    """Raised when a pack manifest does not match the supported schema."""


@dataclass(frozen=True, slots=True)
class TextureRecord:
    """Describe one editable or special texture in a pack."""

    name: str
    editable: str
    special: bool
    alpha: AlphaMode | None = None


@dataclass(frozen=True, slots=True)
class PackManifest:
    """Describe the source archive and textures for one extracted pack."""

    schema_version: int
    archive: str
    textures: list[TextureRecord]


def _require_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"Manifest {field} must be a non-empty string"
        raise ManifestError(msg)
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        msg = f"Manifest {field} must be a safe relative path"
        raise ManifestError(msg)
    return value


def load_manifest(path: Path) -> PackManifest:
    """Load and validate one pack manifest."""
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        msg = "Manifest root must be an object"
        raise ManifestError(msg)
    root = cast("dict[object, object]", value)
    schema_version = root.get("schemaVersion")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        msg = f"Manifest schemaVersion must be {SCHEMA_VERSION}"
        raise ManifestError(msg)

    archive = _require_relative_path(root.get("archive"), "archive")
    raw_textures_value = root.get("textures")
    if not isinstance(raw_textures_value, list):
        msg = "Manifest textures must be an array"
        raise ManifestError(msg)
    raw_textures = cast("list[object]", raw_textures_value)

    textures: list[TextureRecord] = []
    for index, raw_record in enumerate(raw_textures):
        if not isinstance(raw_record, dict):
            msg = f"Manifest textures[{index}] must be an object"
            raise ManifestError(msg)
        record = cast("dict[object, object]", raw_record)
        name = record.get("name")
        special = record.get("special")
        alpha = record.get("alpha")
        if not isinstance(name, str) or not name:
            msg = f"Manifest textures[{index}].name must be a non-empty string"
            raise ManifestError(msg)
        if type(special) is not bool:
            msg = f"Manifest textures[{index}].special must be a boolean"
            raise ManifestError(msg)
        if alpha is not None and alpha not in {
            "none",
            "opaque",
            "transparent",
            "binary",
            "graded",
        }:
            msg = f"Manifest textures[{index}].alpha is invalid"
            raise ManifestError(msg)
        textures.append(
            TextureRecord(
                name=name,
                editable=_require_relative_path(
                    record.get("editable"),
                    f"textures[{index}].editable",
                ),
                special=special,
                alpha=cast("AlphaMode | None", alpha),
            ),
        )
    return PackManifest(
        schema_version=SCHEMA_VERSION,
        archive=archive,
        textures=textures,
    )


def write_manifest(path: Path, archive: str, textures: list[TextureRecord]) -> None:
    """Atomically write one pack manifest."""
    manifest = PackManifest(
        schema_version=SCHEMA_VERSION,
        archive=archive,
        textures=textures,
    )
    texture_documents: list[dict[str, object]] = []
    for texture in manifest.textures:
        record: dict[str, object] = {
            "name": texture.name,
            "editable": texture.editable,
            "special": texture.special,
        }
        if texture.alpha is not None:
            record["alpha"] = texture.alpha
        texture_documents.append(record)
    document = {
        "schemaVersion": manifest.schema_version,
        "archive": manifest.archive,
        "textures": texture_documents,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
