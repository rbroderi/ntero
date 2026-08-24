"""Editable texture alpha contract tests."""

import tempfile
from pathlib import Path

import pytest
from PIL import Image

from ntero.alpha import AlphaMismatchError, AlphaMode, alpha_mode, validate_alpha


def _alpha_image(path: Path, mode: AlphaMode) -> None:
    if mode == "none":
        image = Image.new("RGB", (2, 1), (10, 20, 30))
    else:
        alpha_values = {
            "opaque": (255, 255),
            "transparent": (0, 0),
            "binary": (0, 255),
            "graded": (64, 192),
        }[mode]
        image = Image.new("RGBA", (2, 1), (10, 20, 30, 255))
        image.putalpha(Image.frombytes("L", (2, 1), bytes(alpha_values)))
    image.save(path, format="PNG")


@pytest.mark.parametrize(
    "expected",
    ["none", "opaque", "transparent", "binary", "graded"],
)
def test_classifies_alpha_modes(expected: AlphaMode) -> None:
    """Distinguish absent, constant, binary, and graded alpha information."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "image.png"
        _alpha_image(path, expected)

        assert alpha_mode(path) == expected


def test_classifies_palette_transparency() -> None:
    """Recognize transparency metadata on indexed PNG files."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "indexed.png"
        image = Image.new("P", (1, 1), 0)
        image.putpalette([10, 20, 30] + [0, 0, 0] * 255)
        image.save(path, format="PNG", transparency=0)

        assert alpha_mode(path) == "transparent"


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ("none", "opaque"),
        ("opaque", "none"),
        ("transparent", "transparent"),
        ("binary", "graded"),
        ("graded", "graded"),
    ],
)
def test_accepts_compatible_alpha_changes(
    expected: AlphaMode,
    actual: AlphaMode,
) -> None:
    """Allow equivalent opacity and alpha introduced by binary resampling."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "image.png"
        _alpha_image(path, actual)

        validate_alpha(path, expected)


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ("none", "graded"),
        ("transparent", "binary"),
        ("binary", "opaque"),
        ("graded", "binary"),
    ],
)
def test_rejects_incompatible_alpha_changes(
    expected: AlphaMode,
    actual: AlphaMode,
) -> None:
    """Reject added transparency and loss of required alpha information."""
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "image.png"
        _alpha_image(path, actual)

        with pytest.raises(
            AlphaMismatchError,
            match=f"alpha changed from {expected} to {actual}",
        ):
            validate_alpha(path, expected)
