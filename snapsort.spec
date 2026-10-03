# -*- mode: python ; coding: utf-8 -*-
# snapsort.spec — PyInstaller spec (review 5.3)
#
# Build (Windows Developer PowerShell, or any OS for local testing):
#   uv pip install -e ".[dev]"
#   pyinstaller snapsort.spec --clean
#
# Produces dist/SnapSort/ (one-folder mode: preferred for large Tk apps —
# avoids the 1 GB+ single-exe cost of bundling cv2 + numpy + PIL).
# The GitHub Actions workflow (review 7.1) builds this on windows-latest.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

# Data files the apps need at runtime:
#  - PIL: image format plugins (._imaging*.pyd / libjpeg / libwebp / etc.)
#  - cv2: platform loader + config files
#  - imagehash: ships only code, but collecting is cheap and safe.
added_files = [
    *collect_data_files("PIL"),
    *collect_data_files("cv2"),
    *collect_data_files("imagehash"),
]

# pillow-heif ships a platform-specific _pillow_heif*.so/.pyd that PyInstaller
# may miss; collect its binaries explicitly (review 5.3 note re: RAW is N/A —
# RAW formats are out of scope, so no rawpy/libraw collection needed).
added_binaries = [
    *collect_dynamic_libs("pillow_heif"),
]

a = Analysis(
    ["main.py"],
    pathex=[str(Path(".").resolve())],
    binaries=added_binaries,
    datas=added_files,
    hiddenimports=[
        # Explicitly import (PyInstaller's static analysis misses tk
        # submodules and the heif registration hook).
        "PIL._tkinter_finder",
        "pillow_heif",
        "imagehash",
        "cv2",
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
    ],
    hookspath=[],
    # `cipher` was removed in PyInstaller 6; the kwarg was already unused.
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # one-folder mode (preferred for large apps)
    name="SnapSort",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                    # optional; requires upx.exe on PATH
    console=False,               # no console window on Windows
    # icon="assets/snapsort.ico",  # uncomment once a 256x256 .ico exists
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="SnapSort",
)