"""
analysis_cache.py — Per-file analysis cache (review 3.5)

Persists per-image analysis results (pHash, sharpness, tilt) keyed by
``(path, mtime, size)`` so that re-running the culler over an unchanged
library skips the expensive OpenCV work entirely.

The cache lives at ``<root>/.snapsort_cache.json`` (see
:mod:`paths.CACHE_FILENAME`) and is a plain JSON object::

    {
      "<abs path>": {
          "mtime": 1690000000.123,
          "size": 204800,
          "phash": "ab12...",
          "sharpness": 123.45,
          "tilt_score": 1.2,
          "dominant_axis": "horizontal",
          "line_count": 3
      },
      ...
    }

Entries whose ``mtime`` or ``size`` no longer matches are recomputed and
replaced. The file is written atomically (temp file + rename) and only
after a scan that actually added or updated entries.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

from paths import CACHE_FILENAME

logger = logging.getLogger("snapsort.cache")


class AnalysisCache:
    """In-memory view over the on-disk per-file analysis cache."""

    def __init__(self, root: str):
        self.root = str(Path(root).expanduser().absolute())
        self._path = os.path.join(self.root, CACHE_FILENAME)
        self._entries: Dict[str, dict] = {}
        self._dirty = False
        self.hits = 0
        self.misses = 0
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._entries = {
                    k: v for k, v in data.items() if isinstance(v, dict)
                }
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read cache %s; starting fresh",
                           self._path)

    def save(self) -> None:
        """Atomically persist the cache (only when something changed)."""
        if not self._dirty:
            return
        try:
            self.root = str(Path(self.root).absolute())
            Path(self.root).mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=self.root, prefix=".snapsort_cache.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self._entries, f, indent=1)
                os.replace(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self._dirty = False
            logger.info("Saved analysis cache: %d entries -> %s",
                        len(self._entries), self._path)
        except OSError:
            logger.exception("Failed to write cache %s", self._path)

    # ------------------------------------------------------------------
    # Lookup / update
    # ------------------------------------------------------------------

    def _file_key(self, path: str) -> Optional[tuple]:
        try:
            st = Path(path).stat()
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def lookup(self, path: str) -> Optional[dict]:
        """Return the cached metrics dict for ``path`` if still valid,
        else None (and record a miss)."""
        entry = self._entries.get(str(Path(path).absolute()))
        if entry is None:
            self.misses += 1
            return None
        key = self._file_key(path)
        if key is None:
            self.misses += 1
            return None
        if (entry.get("mtime"), entry.get("size")) != key:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def update(self, path: str, metrics) -> None:
        """Store/refresh the cache entry for ``path`` from ``metrics``."""
        key = self._file_key(path)
        if key is None:
            return
        entry = {
            "mtime": key[0],
            "size": key[1],
            "phash": getattr(metrics, "phash", None),
            "sharpness": getattr(metrics, "sharpness", None),
            "tilt_score": getattr(metrics, "tilt_score", None),
            "dominant_axis": getattr(metrics, "dominant_axis", None),
            "line_count": getattr(metrics, "line_count", 0),
        }
        self._entries[str(Path(path).absolute())] = entry
        self._dirty = True

    def prune_missing(self, existing_paths) -> None:
        """Drop entries whose file no longer exists (keeps the cache small
        across re-organisations)."""
        existing = {str(Path(p).absolute()) for p in existing_paths}
        stale = [k for k in self._entries if k not in existing]
        for k in stale:
            del self._entries[k]
        if stale:
            self._dirty = True
            logger.info("Pruned %d stale cache entries", len(stale))

    # ------------------------------------------------------------------
    # Colour-histogram helpers
    # ------------------------------------------------------------------

    def get_histogram(self, path: str) -> "Optional[list]":
        """Return the cached colour histogram for *path* if still valid, else None."""
        entry = self._entries.get(str(Path(path).absolute()))
        if entry is None:
            return None
        key = self._file_key(path)
        if key is None or (entry.get("mtime"), entry.get("size")) != key:
            return None
        return entry.get("histogram")   # None if not yet stored

    def set_histogram(self, path: str, histogram: list) -> None:
        """Persist *histogram* (list of floats) inside the cache entry for *path*.

        Creates a minimal entry if none exists yet; refreshes mtime/size if the
        file changed since the last full analysis.
        """
        key = self._file_key(path)
        if key is None:
            return
        abs_path = str(Path(path).absolute())
        entry = self._entries.get(abs_path)
        if entry is None or (entry.get("mtime"), entry.get("size")) != key:
            # Either brand-new or stale — start a fresh entry
            entry = {"mtime": key[0], "size": key[1]}
            self._entries[abs_path] = entry
        entry["histogram"] = histogram
        self._dirty = True