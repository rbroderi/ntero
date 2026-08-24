"""Run the command-line interface as a package."""

import ctypes
import multiprocessing
import sys

from ntero.cli import main
from ntero.gui import run_gui


def _streams_are_terminal() -> bool:
    return all(
        stream is not None and stream.isatty()
        for stream in (sys.stdin, sys.stdout, sys.stderr)
    )


def _console_process_count() -> int:
    process_ids = (ctypes.c_ulong * 1)()
    return ctypes.windll.kernel32.GetConsoleProcessList(process_ids, 1)


def _launched_from_terminal() -> bool:
    streams_are_terminal = _streams_are_terminal()
    if not streams_are_terminal or sys.platform != "win32":
        return streams_are_terminal

    own_processes = 2 if getattr(sys, "frozen", False) else 1
    return _console_process_count() > own_processes


def run() -> int:
    """Run the CLI when arguments are supplied, otherwise open SWINGSet."""
    arguments = sys.argv[1:]
    if arguments:
        return main(arguments)

    backend = "textual" if _launched_from_terminal() else "auto"
    return run_gui(backend=backend)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(run())
