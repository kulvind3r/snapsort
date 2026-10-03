"""
deduplicator.py — Exact-duplicate detection and automated discard.

Uses SHA-256 content hashing to find byte-identical photos.  No user input
is needed: all but the first (alphabetically-earliest) copy are moved to
``_Discarded/_ExactDuplicates/`` and journalled for undo.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import journal
from paths import PRUNED_DIRNAMES
from quality import _is_analyzable_file
from widgets import DISCARDED_DIRNAME

logger = logging.getLogger("snapsort.deduplicator")


def get_file_hash(path: str) -> Optional[str]:
    """Return the SHA-256 hex digest of *path*, or None on I/O error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        logger.debug("Cannot hash %s", path, exc_info=True)
        return None


def find_exact_duplicates(folder_path: str) -> List[List[str]]:
    """Return groups of byte-identical image files inside *folder_path*.

    Each group has ≥ 2 paths; the first path is the keeper (alphabetically
    earliest so the original import name is preserved).
    """
    folder = Path(folder_path)
    if not folder.is_dir():
        return []
    files = sorted(
        str(p) for p in folder.iterdir()
        if p.is_file() and _is_analyzable_file(str(p))
    )
    hash_map: Dict[str, List[str]] = {}
    for f in files:
        h = get_file_hash(f)
        if h:
            hash_map.setdefault(h, []).append(f)
    return [paths for paths in hash_map.values() if len(paths) >= 2]


def _unique_dest(dest_dir: str, filename: str) -> str:
    p = Path(filename)
    base, ext = p.stem, p.suffix
    candidate = Path(dest_dir) / filename
    counter = 1
    while candidate.exists():
        candidate = Path(dest_dir) / f"{base}_{counter}{ext}"
        counter += 1
    return str(candidate)


def auto_discard_exact_duplicates(
    root_path: str,
    progress_cb: Optional[Callable[[int, int, int], None]] = None,
) -> Tuple[int, int]:
    """Scan all image folders under *root_path* and auto-discard exact dupes.

    For each group of byte-identical files, keeps the first alphabetically and
    moves the rest to ``_Discarded/_ExactDuplicates/``.

    ``progress_cb(folders_done, folders_total, discarded_so_far)`` is called
    after each folder so the UI can show live progress.

    Returns ``(duplicate_groups_found, total_files_discarded)``.
    """
    root = Path(root_path).expanduser().absolute()
    discarded_dir = root / DISCARDED_DIRNAME / "_ExactDuplicates"

    # Collect folders that contain at least one analysable image
    folders: List[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and entry.name not in PRUNED_DIRNAMES:
                stack.append(entry)
        if any(e.is_file() and _is_analyzable_file(str(e)) for e in entries):
            folders.append(str(current))

    total_folders = len(folders)
    groups_found = 0
    total_discarded = 0

    for i, folder in enumerate(folders, start=1):
        groups = find_exact_duplicates(folder)
        for group in groups:
            groups_found += 1
            dupes = group[1:]  # keep first, discard the rest
            discarded_dir.mkdir(parents=True, exist_ok=True)
            for dupe_path in dupes:
                dest = _unique_dest(str(discarded_dir), Path(dupe_path).name)
                try:
                    journal.safe_move(
                        str(root), dupe_path, dest,
                        op="exact-dedup", restore=dupe_path,
                    )
                    journal.push_undo(
                        str(root),
                        source=dest, original=dupe_path, kind="exact-dedup",
                    )
                    total_discarded += 1
                    logger.info("Exact dupe discarded: %s → %s", dupe_path, dest)
                except (OSError, ValueError) as exc:
                    logger.error("Failed to discard %s: %s", dupe_path, exc)

        if progress_cb is not None:
            try:
                progress_cb(i, total_folders, total_discarded)
            except Exception:
                logger.debug("progress_cb error", exc_info=True)

    logger.info(
        "Exact dedup complete: %d groups found, %d files discarded",
        groups_found, total_discarded,
    )
    return groups_found, total_discarded
