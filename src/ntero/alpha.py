"""Inspect and validate editable texture alpha information."""

from pathlib import Path
from typing import Literal

from PIL import Image

AlphaMode = Literal["none", "opaque", "transparent", "binary", "graded"]


class AlphaMismatchError(ValueError):
    """Raised when an edited texture loses required alpha information."""


def alpha_mode(path: Path) -> AlphaMode:
    """Classify the alpha information stored in one image."""
    with Image.open(path) as image:
        image.load()
        if "A" not in image.getbands() and "transparency" not in image.info:
            return "none"
        histogram = image.convert("RGBA").getchannel("A").histogram()

    populated = {value for value, count in enumerate(histogram) if count}
    if populated == {255}:
        return "opaque"
    if populated == {0}:
        return "transparent"
    if populated <= {0, 255}:
        return "binary"
    return "graded"


def validate_alpha(path: Path, expected: AlphaMode) -> None:
    """Require an edited image to preserve its recorded alpha capability."""
    actual = alpha_mode(path)
    compatible: dict[AlphaMode, set[AlphaMode]] = {
        "none": {"none", "opaque"},
        "opaque": {"none", "opaque"},
        "transparent": {"transparent"},
        "binary": {"binary", "graded"},
        "graded": {"graded"},
    }
    if actual not in compatible[expected]:
        msg = f"Editable texture alpha changed from {expected} to {actual}: {path}"
        raise AlphaMismatchError(msg)
