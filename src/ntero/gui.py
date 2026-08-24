"""SWINGSet interface for the NTERO command workflows."""

from dataclasses import dataclass

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
from swingset import Size
from swingset import Spinner
from swingset import TextField
from swingset import Theme

from ntero.cli import DEFAULT_WORKERS
from ntero.cli import main


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
        self.frame = frame
        self.content = Panel()
        self.content.layout = BoxLayout(BoxLayout.Y_AXIS, gap=8)
        self.command = ComboBox(("extract", "update", "pack", "play"))
        self.library_root = TextField(columns=40)
        self.game_directory = TextField(columns=40)
        self.texture_pack_name = TextField(columns=30)
        self.sound_pack_name = TextField(columns=30)
        self.workers = Spinner(minimum=1, maximum=64, value=DEFAULT_WORKERS)
        self.benchmark = CheckBox("Benchmark")
        self.lossless = CheckBox("Lossless textures")
        self.no_launch = CheckBox("Build overlay only")
        self.benchmark.width = 160
        self.lossless.width = 160
        self.no_launch.width = 160
        self.status = Label("Ready")
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

    def _run_command(self, _event: ActionEvent[Button]) -> None:
        values = self._values()
        try:
            arguments = _arguments(values)
            self.status.text = f"Running {values.command}..."
            main(arguments)
        except (SystemExit, ValueError) as error:
            self.status.text = f"Failed: {error}"
            if App.supports(Capability.MESSAGE_DIALOGS):
                MessageBox.show(
                    str(error),
                    "NTERO",
                    icon=MessageIcon.ERROR,
                    owner=self.frame,
                )
        else:
            self.status.text = f"{values.command.capitalize()} complete"
            if App.supports(Capability.MESSAGE_DIALOGS):
                MessageBox.show(self.status.text, "NTERO", owner=self.frame)

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
        run_button = actions.add(Button("Run"))
        actions.add(self.status)
        actions.height = max(action.height for action in actions)

        self.command.on_change(self._update_options)
        run_button.on_click(self._run_command)
        self._update_options()
        return self.content


def _build_ui() -> None:
    frame = Frame("NTERO")
    frame.size = Size(600, 430)
    frame.add(_NteroForm(frame).build())

    frame.visible = True


def run_gui(*, backend: str = "auto") -> int:
    """Run the SWINGSet interface with the requested backend."""
    App.configure(backend=backend)
    App.set_theme(Theme.CLASSIC)
    App.run(_build_ui)
    return 0
