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
    width: int = THUMB_WIDTH,
    height: int = THUMB_HEIGHT,
) -> Optional[ImageTk.PhotoImage]:
    """Return a center-cropped, downscaled PhotoImage for display.

    Returns None (with a log warning) when the file cannot be decoded.
    Avoids the context-manager form of Image.open so the file handle and
    pixel buffer stay alive until after ``ImageTk.PhotoImage`` is built.
    """
    try:
        src = Image.open(path)
        src.load()
        if src.mode not in ("RGB", "L"):
            src = src.convert("RGB")
        w, h = src.size
        target_ratio = width / height
        src_ratio = w / h
        if src_ratio > target_ratio:
            new_w = max(1, int(h * target_ratio))
            left = (w - new_w) // 2
            img = src.crop((left, 0, left + new_w, h))
        else:
            new_h = max(1, int(w / target_ratio))
            top = (h - new_h) // 2
            img = src.crop((0, top, w, top + new_h))
        img = img.resize((width, height), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:
        logger.warning("Could not render thumbnail for %s", path, exc_info=True)
        return None
