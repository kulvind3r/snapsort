"""
paths.py — Data-safety path helpers (Section 8 of the review)

Central place for the directory/journal naming contract and the path
containment guard. The invariants enforced here are:

* ``DISCARDED_DIRNAME`` (``_Discarded``) is the single top-level folder
  that receives rejected photos: ``<output_root>/_Discarded/<event>/<file>``.
* ``JOURNAL_FILENAME`` is the append-only move journal kept at the output
  root (the source of truth for undo).
* ``UNDO_FILENAME`` is the persisted undo stack for the culling UI.
* ``is_within(child, parent)`` is the containment check that must pass
  before *any* ``shutil.move`` executes (blocks symlink traversal and
  adversarial absolute paths).
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

PathLike = Union[str, Path]

# ---------------------------------------------------------------------------
# Naming contract
# ---------------------------------------------------------------------------

#: Top-level folder for rejected/discarded photos (review 3.1).
DISCARDED_DIRNAME = "_Discarded"

#: Legacy per-folder rejects directory. No longer written, but pruned by
#: scanners so old libraries keep working (review 3.1).
REJECTS_DIRNAME = "_Rejects"

#: Append-only move journal at the output root (review section 8, item 4).
JOURNAL_FILENAME = ".snapsort_journal.json"

#: Persisted undo stack at the output root (review 3.1).
UNDO_FILENAME = ".snapsort_undo.json"

#: Per-file analysis cache at the output root (review 3.5).
CACHE_FILENAME = ".snapsort_cache.json"

#: Directory names that are never scanned as photo sources.
PRUNED_DIRNAMES = {DISCARDED_DIRNAME, REJECTS_DIRNAME}


# ---------------------------------------------------------------------------
# Containment guard
# ---------------------------------------------------------------------------

def is_within(child: PathLike, parent: PathLike) -> bool:
    """Return True if ``child`` (resolved) is ``parent`` itself or a
    descendant of ``parent`` (resolved).

    Resolution through ``Path.resolve()`` neutralises symlink escapes: an
    adversarial symlink inside the library that points outside
    ``parent`` fails this check.
    """
    try:
        c = Path(child).resolve()
        p = Path(parent).resolve()
    except (OSError, RuntimeError):
        return False
    return c == p or p in c.parents


def assert_within(child: PathLike, parent: PathLike) -> Path:
    """Like :func:`is_within` but raises ``ValueError`` on escape.

    Returns the resolved child path for convenient chaining.
    """
    resolved = Path(child).resolve()
    if not is_within(resolved, parent):
        raise ValueError(
            f"Path containment violated: {resolved} is outside {Path(parent).resolve()}"
        )
    return resolved


def discarded_path(output_root: PathLike, event_folder: str,
                   filename: str) -> Path:
    """Destination for a rejected photo:
    ``<output_root>/_Discarded/<event_folder>/<filename>``."""
    return Path(output_root) / DISCARDED_DIRNAME / event_folder / filename


def journal_path(output_root: PathLike) -> Path:
    return Path(output_root) / JOURNAL_FILENAME


def undo_path(output_root: PathLike) -> Path:
    return Path(output_root) / UNDO_FILENAME


def cache_path(output_root: PathLike) -> Path:
    return Path(output_root) / CACHE_FILENAME