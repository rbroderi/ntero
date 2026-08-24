"""Locate data files bundled with the package or a source checkout."""

from pathlib import Path


def resource_path(relative: str | Path) -> Path:
    """Return an existing package resource path for the current runtime layout."""
    requested = Path(relative)
    if (
        requested.is_absolute()
        or not requested.parts
        or any(part in {"", ".", ".."} for part in requested.parts)
    ):
        msg = f"Resource path must be safe and relative: {relative}"
        raise ValueError(msg)

    package_root = Path(__file__).resolve().parent
    roots = [package_root]
    if package_root.parent.name == "src":
        roots.append(package_root.parent.parent)
    for root in roots:
        candidate = root / requested
        if candidate.is_file():
            return candidate.resolve()

    msg = f"Packaged resource is missing: {requested.as_posix()}"
    raise FileNotFoundError(msg)
