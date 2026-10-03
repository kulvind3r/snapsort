"""
widgets.py — UI layout constants and the thumbnail helper for SnapSort.
"""

from __future__ import annotations

import logging
from typing import Optional

from PIL import Image, ImageTk

import paths

logger = logging.getLogger("snapsort.widgets")

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

THUMB_WIDTH = 380
THUMB_HEIGHT = 300

DISCARDED_DIRNAME = paths.DISCARDED_DIRNAME

BG = "#1e1e1e"
FG = "#e0e0e0"
ACCENT = "#4fc3f7"
BEST_BG = "#2e5d3b"
BEST_FG = "#b9f6ca"
PANEL_BG = "#252526"
DIM = "#9e9e9e"


def make_thumbnail(
    path: str,
    max_w: int = 380,
    max_h: int = 300,
) -> Optional[ImageTk.PhotoImage]:
    """Return a PhotoImage scaled to fit within ``max_w × max_h``, aspect ratio preserved.

    Uses ``Image.thumbnail`` (fit-within / letterbox) so the full photo is
    always visible with no cropping.  Returns None on failure.
    """
    try:
        src = Image.open(path)
        src.load()
        if src.mode not in ("RGB", "L"):
            src = src.convert("RGB")
        src.thumbnail((max_w, max_h), Image.LANCZOS)
        return ImageTk.PhotoImage(src)
    except Exception:
        logger.warning("Could not render thumbnail for %s", path, exc_info=True)
        return None
