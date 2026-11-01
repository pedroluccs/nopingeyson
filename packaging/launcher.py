"""Ponto de entrada do NoPingeyson.exe (PyInstaller)."""

import multiprocessing
import sys

from nopingeyson.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())