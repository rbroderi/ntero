"""Run the command-line interface as a package."""

import multiprocessing

from ntero.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
