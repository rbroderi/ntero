"""Bundled resource resolution tests."""

import pytest

from ntero.resources import resource_path


def test_resolves_checkout_resources() -> None:
    """Find resources from the repository when running the source package."""
    assert resource_path("pyproject.toml").name == "pyproject.toml"


def test_rejects_unsafe_resource_paths() -> None:
    """Prevent resource lookups from escaping the package or checkout root."""
    with pytest.raises(ValueError, match="safe and relative"):
        resource_path("../pyproject.toml")


def test_reports_missing_resource() -> None:
    """Report a missing bundled file after checking every runtime root."""
    with pytest.raises(FileNotFoundError, match=r"missing\.txt"):
        resource_path("not-present/missing.txt")
