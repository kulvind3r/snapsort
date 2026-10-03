# -*- mode: python ; coding: utf-8 -*-
# snapsort.spec — PyInstaller spec: single-file Windows executable.
#
# Build (on windows-latest runner via GitHub Actions, or a Windows machine):
#   uv pip install -e ".[dev]"
#   pyinstaller snapsort.spec --clean
#
# Output: dist/SnapSort.exe  (single file, no _internal folder)
#
# First-launch note: PyInstaller single-file mode extracts to a temp folder
# on the first run (~2-5 s). Subsequent launches reuse the extracted copy
# and start immediately.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

added_files = [
    *collect_data_files("PIL"),       # JPEG/PNG/WebP/HEIF codec plugins
    *collect_data_files("cv2"),       # OpenCV platform loader + config
    *collect_data_files("imagehash"), # safe to collect; adds almost nothing
]

# pillow-heif ships a compiled extension that PyInstaller's static analysis
# can miss — collect it explicitly so HEIC/HEIF photos work in the binary.
added_binaries = [
    *collect_dynamic_libs("pillow_heif"),
]

a = Analysis(
    ["main.py"],
    pathex=[str(Path(".").resolve())],
    binaries=added_binaries,
    datas=added_files,
    hiddenimports=[
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
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# Single-file mode: pass binaries + datas into EXE directly.
# exclude_binaries=False bundles everything; COLLECT is not used.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,   # bundled into the exe
    a.zipfiles,
    a.datas,      # bundled into the exe
    exclude_binaries=False,
    name="SnapSort",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                   # optional: requires upx.exe on PATH; ~20% smaller
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,              # no terminal window
    # icon="assets/snapsort.ico",  # uncomment once a 256×256 .ico file exists
)
