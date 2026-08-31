"""Tests for the SWINGSet launch form."""

import json
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from swingset import Frame
from swingset import Theme

from ntero.__main__ import _launched_from_terminal
from ntero.__main__ import run
from ntero.gui import _arguments
from ntero.gui import _FormValues
from ntero.gui import _load_saved_values
from ntero.gui import _NteroForm
from ntero.gui import _save_values
from ntero.gui import _SavedValues
from ntero.gui import run_gui

EXPECTED_CLI_EXIT = 4
SAVED_WORKERS = 3


def test_form_prefills_saved_values() -> None:
    """Initialize every control from the previous valid submission."""
    saved = _SavedValues(
        command="play",
        library_root="library",
        game_directory="game",
        texture_pack_name="textures",
        sound_pack_name="sounds",
        workers=SAVED_WORKERS,
        benchmark=True,
        lossless=True,
        no_launch=True,
    )

    with patch("ntero.gui._load_saved_values", return_value=saved):
        form = _NteroForm(Frame("NTERO"))

    assert form.command.selected_item == "play"
    assert form.library_root.text == "library"
    assert form.game_directory.text == "game"
    assert form.texture_pack_name.text == "textures"
    assert form.sound_pack_name.text == "sounds"
    assert form.workers.value == SAVED_WORKERS
    assert form.benchmark.selected
    assert form.lossless.selected
    assert form.no_launch.selected


def test_command_output_is_captured_in_output_pane() -> None:
    """Show command stdout and stderr before reporting completion."""
    form = _NteroForm(Frame("NTERO"))
    process = SimpleNamespace(stdout=StringIO("extracting archive\ndecoder note\n"))

    def wait() -> int:
        assert "extracting archive" in form.output.text
        return 0

    process.wait = wait
    with (
        patch("ntero.gui.App.supports", return_value=False),
        patch(
            "ntero.gui.UIThread.invoke_later",
            side_effect=lambda callback: callback(),
        ),
        patch("ntero.gui.subprocess.Popen", return_value=process) as popen,
    ):
        form.build()
        form._execute_command("extract", ["extract"])

    popen.assert_called_once()
    assert "extracting archive" in form.output.text
    assert "decoder note" in form.output.text
    assert "Extract complete" in form.output.text
    assert form.status.text == "Extract complete"
    assert form.run_button.enabled


def test_command_failure_is_reported_in_output_pane() -> None:
    """Leave command errors visible in the output pane."""
    form = _NteroForm(Frame("NTERO"))
    process = SimpleNamespace(stdout=StringIO("invalid archive\n"))
    process.wait = lambda: 2
    with (
        patch("ntero.gui.App.supports", return_value=False),
        patch(
            "ntero.gui.UIThread.invoke_later",
            side_effect=lambda callback: callback(),
        ),
        patch("ntero.gui.subprocess.Popen", return_value=process),
    ):
        form.build()
        form._execute_command("extract", ["extract"])

    assert "invalid archive" in form.output.text
    assert "Failed: Command exited with code 2" in form.output.text
    assert form.status.text == "Failed: Command exited with code 2"
    assert form.run_button.enabled


def test_output_pane_follows_new_output() -> None:
    """Grow and scroll the output viewport as command lines accumulate."""
    form = _NteroForm(Frame("NTERO"))
    form._append_output("\n".join(f"line {index}" for index in range(20)))

    assert form.output.height > form.output_pane.height
    assert form.output_pane.scroll_position[1] > 0


def test_saved_values_round_trip(tmp_path: Path) -> None:
    """Persist the complete form state in the user settings file."""
    values = _FormValues(
        command="play",
        library_root=" library ",
        game_directory=" game ",
        texture_pack_name="textures",
        sound_pack_name="sounds",
        workers=SAVED_WORKERS,
        benchmark=True,
        lossless=True,
        no_launch=True,
    )
    settings = tmp_path / "gui-settings.json"

    with patch("ntero.gui._settings_path", return_value=settings):
        _save_values(values)
        loaded = _load_saved_values()

    assert loaded == _SavedValues(
        command="play",
        library_root="library",
        game_directory="game",
        texture_pack_name="textures",
        sound_pack_name="sounds",
        workers=SAVED_WORKERS,
        benchmark=True,
        lossless=True,
        no_launch=True,
    )
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "command": "play",
        "libraryRoot": "library",
        "gameDirectory": "game",
        "texturePackName": "textures",
        "soundPackName": "sounds",
        "workers": SAVED_WORKERS,
        "benchmark": True,
        "lossless": True,
        "noLaunch": True,
    }


@pytest.mark.parametrize("content", ["not json", "[]", '{"libraryRoot": 1}'])
def test_saved_values_ignore_malformed_settings(tmp_path: Path, content: str) -> None:
    """Treat corrupt or incorrectly typed settings as unset paths."""
    settings = tmp_path / "gui-settings.json"
    settings.write_text(content, encoding="utf-8")

    with patch("ntero.gui._settings_path", return_value=settings):
        loaded = _load_saved_values()

    assert loaded == _SavedValues()


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
