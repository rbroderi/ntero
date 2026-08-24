"""Tests for the SWINGSet launch form."""

import sys
from unittest.mock import patch

import pytest
from swingset import Frame
from swingset import Theme

from ntero.__main__ import _launched_from_terminal
from ntero.__main__ import run
from ntero.gui import _arguments
from ntero.gui import _FormValues
from ntero.gui import _NteroForm
from ntero.gui import run_gui

EXPECTED_CLI_EXIT = 4


def test_run_gui_uses_classic_theme() -> None:
    """Configure the selected backend with SWINGSet's classic theme."""
    with (
        patch("ntero.gui.App.configure") as configure,
        patch("ntero.gui.App.set_theme") as set_theme,
        patch("ntero.gui.App.run") as app_run,
    ):
        assert run_gui(backend="auto") == 0

    configure.assert_called_once_with(backend="auto")
    set_theme.assert_called_once_with(Theme.CLASSIC)
    app_run.assert_called_once()


def test_arguments_builds_extract_cli() -> None:
    """Translate form fields into the existing extract interface."""
    values = _FormValues(
        command="extract",
        library_root="library",
        game_directory="game",
        texture_pack_name="textures",
        sound_pack_name="sounds",
        workers=3,
        benchmark=True,
        lossless=False,
        no_launch=False,
    )

    assert _arguments(values) == [
        "extract",
        "--library-root",
        "library",
        "--texture-pack-name",
        "textures",
        "--sound-pack-name",
        "sounds",
        "--workers",
        "3",
        "--game-dir",
        "game",
        "--benchmark",
    ]


def test_arguments_builds_play_cli_without_irrelevant_options() -> None:
    """Include only options accepted by the selected command."""
    values = _FormValues(
        command="play",
        library_root="library",
        game_directory="game",
        texture_pack_name="textures",
        sound_pack_name="",
        workers=3,
        benchmark=True,
        lossless=True,
        no_launch=True,
    )

    assert _arguments(values) == [
        "play",
        "--library-root",
        "library",
        "--texture-pack-name",
        "textures",
        "--game-dir",
        "game",
        "--no-launch",
    ]


def test_arguments_requires_a_pack() -> None:
    """Reject a form submission with no selected asset pack."""
    values = _FormValues(
        command="pack",
        library_root="library",
        game_directory="",
        texture_pack_name="",
        sound_pack_name="",
        workers=2,
        benchmark=False,
        lossless=False,
        no_launch=False,
    )

    with pytest.raises(ValueError, match="pack name"):
        _arguments(values)


@pytest.mark.parametrize(
    ("file_dialogs", "expected_children"),
    [(False, 2), (True, 3)],
)
def test_directory_row_only_offers_supported_browse_dialog(
    *,
    file_dialogs: bool,
    expected_children: int,
) -> None:
    """Keep terminal paths editable without exposing unsupported dialogs."""
    form = _NteroForm(Frame("NTERO"))
    with patch("ntero.gui.App.supports", return_value=file_dialogs):
        row = form._add_directory_row("Library", form.library_root)

    assert len(row.children) == expected_children


@pytest.mark.parametrize(
    ("terminal_launch", "expected_backend"),
    [(True, "textual"), (False, "auto")],
)
def test_run_selects_gui_backend(
    *,
    terminal_launch: bool,
    expected_backend: str,
) -> None:
    """Select terminal UI only when launched from an existing terminal."""
    with (
        patch.object(sys, "argv", ["ntero"]),
        patch("ntero.__main__._launched_from_terminal", return_value=terminal_launch),
        patch("ntero.__main__.run_gui", return_value=0) as run_gui,
    ):
        assert run() == 0

    run_gui.assert_called_once_with(backend=expected_backend)


@pytest.mark.parametrize(
    ("frozen", "process_count", "expected"),
    [
        (False, 2, True),
        (True, 2, False),
        (True, 3, True),
    ],
)
def test_terminal_detection_ignores_pyinstaller_parent(
    *,
    frozen: bool,
    process_count: int,
    expected: bool,
) -> None:
    """Do not mistake PyInstaller's bootloader parent for an invoking shell."""
    with (
        patch.object(sys, "platform", "win32"),
        patch.object(sys, "frozen", frozen, create=True),
        patch("ntero.__main__._streams_are_terminal", return_value=True),
        patch("ntero.__main__._console_process_count", return_value=process_count),
    ):
        assert _launched_from_terminal() is expected


def test_run_preserves_cli_arguments() -> None:
    """Bypass the GUI whenever command-line arguments are supplied."""
    arguments = ["pack", "--library-root", "library"]
    with (
        patch.object(sys, "argv", ["ntero", *arguments]),
        patch("ntero.__main__.main", return_value=EXPECTED_CLI_EXIT) as cli_main,
        patch("ntero.__main__.run_gui") as run_gui,
    ):
        assert run() == EXPECTED_CLI_EXIT

    cli_main.assert_called_once_with(arguments)
    run_gui.assert_not_called()
