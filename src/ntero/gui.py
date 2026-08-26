"""SWINGSet interface for the NTERO command workflows."""

import json
import os
import sys
from collections.abc import Callable
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from contextlib import suppress
from dataclasses import dataclass
from io import TextIOBase
from pathlib import Path
from threading import Lock
from threading import Thread

from swingset import ActionEvent
from swingset import App
from swingset import BoxLayout
from swingset import Button
from swingset import Capability
from swingset import CheckBox
from swingset import ComboBox
from swingset import Component
from swingset import FolderBrowserDialog
from swingset import Frame
from swingset import Label
from swingset import MessageBox
from swingset import MessageIcon
from swingset import Panel
from swingset import RichTextPane
from swingset import ScrollPane
from swingset import Size
from swingset import Spinner
from swingset import TextField
from swingset import Theme
from swingset import UIThread

from ntero.cli import DEFAULT_WORKERS
from ntero.cli import main

_SETTINGS_DIRECTORY = "NTERO"
_SETTINGS_FILE = "gui-settings.json"
_COMMANDS = ("extract", "update", "pack", "play")
_MAX_WORKERS = 64
_OUTPUT_ROWS = 10
_OUTPUT_COLUMNS = 68
_OUTPUT_LINE_HEIGHT = 20


class _OutputWriter(TextIOBase):
    def __init__(self, append: Callable[[str], None]) -> None:
        super().__init__()
        self._append = append
        self._lock = Lock()
        self._pending: list[str] = []
        self._scheduled = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if not value:
            return 0
        with self._lock:
            self._pending.append(value)
            if self._scheduled:
                return len(value)
            self._scheduled = True
        UIThread.invoke_later(self._drain)
        return len(value)

    def _drain(self) -> None:
        with self._lock:
            value = "".join(self._pending)
            self._pending.clear()
            self._scheduled = False
        self._append(value)

    def isatty(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _FormValues:
    command: str
    library_root: str
    game_directory: str
    texture_pack_name: str
    sound_pack_name: str
    workers: int
    benchmark: bool
    lossless: bool
    no_launch: bool


@dataclass(frozen=True, slots=True)
class _SavedValues:
    command: str = "extract"
    library_root: str = ""
    game_directory: str = ""
    texture_pack_name: str = ""
    sound_pack_name: str = ""
    workers: int = DEFAULT_WORKERS
    benchmark: bool = False
    lossless: bool = False
    no_launch: bool = False


def _settings_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / _SETTINGS_DIRECTORY / _SETTINGS_FILE
    return Path.home() / ".config" / _SETTINGS_DIRECTORY / _SETTINGS_FILE


def _load_saved_values() -> _SavedValues:
    try:
        document = json.loads(_settings_path().read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return _SavedValues()
    if not isinstance(document, dict):
        return _SavedValues()
    command = document.get("command")
    library_root = document.get("libraryRoot")
    game_directory = document.get("gameDirectory")
    texture_pack_name = document.get("texturePackName")
    sound_pack_name = document.get("soundPackName")
    workers = document.get("workers")
    benchmark = document.get("benchmark")
    lossless = document.get("lossless")
    no_launch = document.get("noLaunch")
    return _SavedValues(
        command=command if command in _COMMANDS else "extract",
        library_root=library_root if isinstance(library_root, str) else "",
        game_directory=game_directory if isinstance(game_directory, str) else "",
        texture_pack_name=(
            texture_pack_name if isinstance(texture_pack_name, str) else ""
        ),
        sound_pack_name=sound_pack_name if isinstance(sound_pack_name, str) else "",
        workers=(
            workers
            if isinstance(workers, int)
            and not isinstance(workers, bool)
            and 1 <= workers <= _MAX_WORKERS
            else DEFAULT_WORKERS
        ),
        benchmark=benchmark if isinstance(benchmark, bool) else False,
        lossless=lossless if isinstance(lossless, bool) else False,
        no_launch=no_launch if isinstance(no_launch, bool) else False,
    )


def _save_values(values: _FormValues) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "command": values.command,
                "libraryRoot": values.library_root.strip(),
                "gameDirectory": values.game_directory.strip(),
                "texturePackName": values.texture_pack_name.strip(),
                "soundPackName": values.sound_pack_name.strip(),
                "workers": values.workers,
                "benchmark": values.benchmark,
                "lossless": values.lossless,
                "noLaunch": values.no_launch,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _arguments(values: _FormValues) -> list[str]:
    library_root = _required(values.library_root, "Select a library directory")
    texture_pack_name = values.texture_pack_name.strip()
    sound_pack_name = values.sound_pack_name.strip()
    if not texture_pack_name and not sound_pack_name:
        msg = "Enter a texture pack name, a sound pack name, or both"
        raise ValueError(msg)

    arguments = [values.command, "--library-root", library_root]
    if texture_pack_name:
        arguments.extend(("--texture-pack-name", texture_pack_name))
    if sound_pack_name:
        arguments.extend(("--sound-pack-name", sound_pack_name))
    if values.command != "play":
        arguments.extend(("--workers", str(values.workers)))
    if values.command in {"extract", "update", "play"}:
        game_directory = _required(values.game_directory, "Select the game directory")
        arguments.extend(("--game-dir", game_directory))
    if values.benchmark and values.command != "play":
        arguments.append("--benchmark")
    if values.lossless and values.command == "pack":
        arguments.append("--lossless")
    if values.no_launch and values.command == "play":
        arguments.append("--no-launch")
    return arguments


class _NteroForm:
    def __init__(self, frame: Frame) -> None:
        saved_values = _load_saved_values()
        self.frame = frame
        self.content = Panel()
        self.content.layout = BoxLayout(BoxLayout.Y_AXIS, gap=8)
        self.command = ComboBox(_COMMANDS)
        self.command.selected_index = _COMMANDS.index(saved_values.command)
        self.library_root = TextField(columns=40)
        self.library_root.text = saved_values.library_root
        self.game_directory = TextField(columns=40)
        self.game_directory.text = saved_values.game_directory
        self.texture_pack_name = TextField(columns=30)
        self.texture_pack_name.text = saved_values.texture_pack_name
        self.sound_pack_name = TextField(columns=30)
        self.sound_pack_name.text = saved_values.sound_pack_name
        self.workers = Spinner(
            minimum=1,
            maximum=_MAX_WORKERS,
            value=saved_values.workers,
        )
        self.benchmark = CheckBox("Benchmark")
        self.benchmark.selected = saved_values.benchmark
        self.lossless = CheckBox("Lossless textures")
        self.lossless.selected = saved_values.lossless
        self.no_launch = CheckBox("Build overlay only")
        self.no_launch.selected = saved_values.no_launch
        self.benchmark.width = 160
        self.lossless.width = 160
        self.no_launch.width = 160
        self.status = Label("Ready")
        self.output = RichTextPane(
            rows=_OUTPUT_ROWS,
            columns=_OUTPUT_COLUMNS,
            editable=False,
        )
        self.output_pane = ScrollPane(self.output)
        self.output_pane.size = self.output.size
        self.run_button: Button
        self.game_row: Panel
        self.workers_row: Panel

    def _add_row(self, label: str, field: Component) -> Panel:
        row = self.content.add(Panel())
        row.layout = BoxLayout(BoxLayout.X_AXIS, gap=8)
        caption = row.add(Label(label))
        caption.width = 120
        row.add(field)
        row.height = field.height
        return row

    def _add_directory_row(self, label: str, field: TextField) -> Panel:
        row = self._add_row(label, field)
        if not App.supports(Capability.FILE_DIALOGS):
            return row

        button = row.add(Button("Browse"))
        row.height = max(row.height, button.height)

        def choose_directory(_event: ActionEvent[Button]) -> None:
            selected = FolderBrowserDialog(f"Select {label}").show(self.frame)
            if selected is not None:
                field.text = str(selected)

        button.on_click(choose_directory)
        return row

    def _selected_command(self) -> str:
        return self.command.selected_item or "extract"

    def _update_options(self, _event: object | None = None) -> None:
        selected = self._selected_command()
        self.game_row.visible = selected != "pack"
        self.workers_row.visible = selected != "play"
        self.benchmark.visible = selected != "play"
        self.lossless.visible = selected == "pack"
        self.no_launch.visible = selected == "play"

    def _values(self) -> _FormValues:
        return _FormValues(
            command=self._selected_command(),
            library_root=self.library_root.text,
            game_directory=self.game_directory.text,
            texture_pack_name=self.texture_pack_name.text,
            sound_pack_name=self.sound_pack_name.text,
            workers=self.workers.value,
            benchmark=self.benchmark.selected,
            lossless=self.lossless.selected,
            no_launch=self.no_launch.selected,
        )

    def _append_output(self, value: str) -> None:
        text = self.output.snapshot_text() + value
        self.output.text = text
        lines = text.splitlines() or [""]
        visual_lines = sum(
            max(1, (len(line) + _OUTPUT_COLUMNS - 1) // _OUTPUT_COLUMNS)
            for line in lines
        )
        self.output.height = max(
            self.output_pane.height,
            visual_lines * _OUTPUT_LINE_HEIGHT,
        )
        self.output.selection_range = (len(text), len(text))
        self.output_pane.scroll_to(0, self.output.height)

    def _finish_command(self, command: str, error: BaseException | None) -> None:
        self.run_button.enabled = True
        if error is not None:
            self.status.text = f"Failed: {error}"
            self._append_output(f"\n{self.status.text}\n")
            if App.supports(Capability.MESSAGE_DIALOGS):
                MessageBox.show(
                    str(error),
                    "NTERO",
                    icon=MessageIcon.ERROR,
                    owner=self.frame,
                )
            return

        self.status.text = f"{command.capitalize()} complete"
        self._append_output(f"\n{self.status.text}\n")
        if App.supports(Capability.MESSAGE_DIALOGS):
            MessageBox.show(self.status.text, "NTERO", owner=self.frame)

    def _execute_command(self, command: str, arguments: list[str]) -> None:
        error: BaseException | None = None
        writer = _OutputWriter(self._append_output)
        try:
            with redirect_stdout(writer), redirect_stderr(writer):
                main(arguments)
        except SystemExit as caught:
            error = caught
        except Exception as caught:  # noqa: BLE001
            error = caught
        finally:
            UIThread.invoke_later(lambda: self._finish_command(command, error))

    def _run_command(self, _event: ActionEvent[Button]) -> None:
        values = self._values()
        try:
            arguments = _arguments(values)
        except ValueError as error:
            self.status.text = f"Failed: {error}"
            self._append_output(f"{self.status.text}\n")
            if App.supports(Capability.MESSAGE_DIALOGS):
                MessageBox.show(
                    str(error),
                    "NTERO",
                    icon=MessageIcon.ERROR,
                    owner=self.frame,
                )
            return

        with suppress(OSError):
            _save_values(values)
        self.output.text = ""
        self.status.text = f"Running {values.command}..."
        self._append_output(f"{self.status.text}\n")
        self.run_button.enabled = False
        Thread(
            target=self._execute_command,
            args=(values.command, arguments),
            daemon=True,
            name="ntero-command",
        ).start()

    def build(self) -> Panel:
        self._add_row("Command", self.command)
        self._add_directory_row("Library", self.library_root)
        self.game_row = self._add_directory_row(
            "Game directory",
            self.game_directory,
        )
        self._add_row("Texture pack", self.texture_pack_name)
        self._add_row("Sound pack", self.sound_pack_name)
        self.workers_row = self._add_row("Workers", self.workers)

        options = self.content.add(Panel())
        options.layout = BoxLayout(BoxLayout.X_AXIS, gap=8)
        options.add(self.benchmark)
        options.add(self.lossless)
        options.add(self.no_launch)
        options.height = max(option.height for option in options)

        actions = self.content.add(Panel())
        actions.layout = BoxLayout(BoxLayout.X_AXIS, gap=8)
        self.run_button = actions.add(Button("Run"))
        actions.add(self.status)
        actions.height = max(action.height for action in actions)

        self._add_row("Output", self.output_pane)

        self.command.on_change(self._update_options)
        self.run_button.on_click(self._run_command)
        self._update_options()
        return self.content


def _build_ui() -> None:
    frame = Frame("NTERO")
    frame.size = Size(600, 640)
    frame.add(_NteroForm(frame).build())

    frame.visible = True


def run_gui(*, backend: str = "auto") -> int:
    """Run the SWINGSet interface with the requested backend."""
    App.configure(backend=backend)
    App.set_theme(Theme.CLASSIC)
    App.run(_build_ui)
    return 0
