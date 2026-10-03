"""
journal.py — Append-only move journal + persisted undo stack

Implements the data-safety contract (review Section 8):

* Every ``shutil.move`` is journaled **before** it executes as
  ``{op, src, dst, timestamp, restore, event}`` in
  ``<output_root>/.snapsort_journal.json``. The journal is append-only and
  is the source of truth for undo.
* The culling UI's undo stack is persisted to
  ``<output_root>/.snapsort_undo.json`` so a crash or restart does not
  lose undo history (review 3.1).
* No file is ever deleted anywhere in this module; writes are atomic
  (write to temp file + ``os.replace``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import paths

logger = logging.getLogger("snapsort.journal")

PathLike = Union[str, Path]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically (temp file in same dir + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        # Never let journal bookkeeping take the app down; the move itself
        # is what matters. Clean up the temp file if it lingers.
        logger.exception("Failed writing %s", path)
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load_json_list(path: Path) -> List[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("Corrupt journal file %s; starting fresh", path)
        return []
    return []


# ---------------------------------------------------------------------------
# Move journal
# ---------------------------------------------------------------------------

def load_journal(output_root: PathLike) -> List[Dict[str, Any]]:
    """Return the list of journal entries (newest last)."""
    return _load_json_list(paths.journal_path(output_root))


def record_move(output_root: PathLike, src: PathLike, dst: PathLike,
                op: str = "move", restore: Optional[str] = None,
                event: Optional[str] = None) -> Dict[str, Any]:
    """Append a move entry to the journal **before** the move executes.

    ``restore`` is the path the file should go back to on undo; ``event``
    names the event folder it belonged to.
    """
    entry = {
        "op": op,
        "src": str(src),
        "dst": str(dst),
        "timestamp": _now_iso(),
        "restore": str(restore) if restore else None,
        "event": event,
    }
    jpath = paths.journal_path(output_root)
    entries = load_journal(output_root)
    entries.append(entry)
    _atomic_write_json(jpath, entries)
    return entry


# ---------------------------------------------------------------------------
# Safe move (journal + containment + logging, then move)
# ---------------------------------------------------------------------------

def safe_move(output_root: PathLike, src: PathLike, dst: PathLike,
              op: str = "move", restore: Optional[str] = None,
              event: Optional[str] = None,
              contain_root: Optional[PathLike] = None) -> None:
    """Journal, validate, log and perform a single ``shutil.move``.

    Raises
    ------
    ValueError
        If ``dst`` (or ``src``) resolves outside ``contain_root``
        (defaults to ``output_root``) — the data-safety containment guard.
    """
    src_p, dst_p = Path(src), Path(dst)
    root = contain_root if contain_root is not None else output_root

    if not paths.is_within(dst_p, root):
        logger.error(
            "Refusing move — destination escapes output root: %s -> %s",
            src_p, dst_p,
        )
        raise ValueError(
            f"Destination {dst_p.resolve()} is outside {Path(root).resolve()}"
        )
    if not paths.is_within(src_p, root):
        logger.error(
            "Refusing move — source escapes output root: %s -> %s",
            src_p, dst_p,
        )
        raise ValueError(
            f"Source {src_p.resolve()} is outside {Path(root).resolve()}"
        )

    record_move(output_root, src_p, dst_p, op=op, restore=restore, event=event)
    logger.info("Moving %s -> %s", src_p, dst_p)
    try:
        shutil.move(str(src_p), str(dst_p))
    except OSError:
        logger.error("Move failed: %s -> %s", src_p, dst_p, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Persisted undo stack (UI)
# ---------------------------------------------------------------------------

def load_undo_stack(output_root: PathLike) -> List[Dict[str, str]]:
    """Load persisted undo entries (newest last)."""
    return _load_json_list(paths.undo_path(output_root))


def push_undo(output_root: PathLike, source: str, original: str,
              kind: str = "reject") -> None:
    """Append one undo entry and persist immediately."""
    stack = load_undo_stack(output_root)
    stack.append({"source": source, "original": original, "kind": kind})
    _atomic_write_json(paths.undo_path(output_root), stack)


def pop_undo(output_root: PathLike) -> Optional[Dict[str, str]]:
    """Pop the most recent undo entry and persist the shortened stack."""
    stack = load_undo_stack(output_root)
    if not stack:
        return None
    entry = stack.pop()
    _atomic_write_json(paths.undo_path(output_root), stack)
    return entry