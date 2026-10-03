"""
organizer.py — Stage 1 orchestration: scan → cluster → move.

Delegates metadata extraction to ``metadata`` and date/trip clustering to
``clustering``. This module owns only the file-move pipeline and the
public API surface consumed by ``main`` and the test suite.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import date

import config
import journal
import paths
from metadata import (  # re-exported for callers that import from organizer
    PhotoRecord,
    SUPPORTED_EXTENSIONS,
    discover_photos,
    get_image_date,
    get_image_gps,
    _populate_metadata,
)
from clustering import (  # re-exported for callers that import from organizer
    cluster_records,
    _folder_name,
    haversine,
    centroid,
)

logger = logging.getLogger("snapsort.organizer")


def _resolve_unique_destination(dest_dir: str, filename: str) -> str:
    """Return a non-colliding destination path (appends _1, _2, …)."""
    p = Path(filename)
    base, ext = p.stem, p.suffix
    d = Path(dest_dir)
    candidate = d / filename
    counter = 1
    while candidate.exists():
        candidate = d / f"{base}_{counter}{ext}"
        counter += 1
    return str(candidate)


def organize_directory(
    root_dir: str,
    output_dir: Optional[str] = None,
    dry_run: bool = False,
    config_obj: Optional[dict] = None,
) -> List[PhotoRecord]:
    """Run the full Stage-1 pipeline: discover → cluster → move.

    Parameters
    ----------
    root_dir:
        Directory to scan. Files are moved in-place when ``output_dir``
        is None.
    output_dir:
        Optional separate target root for organized folders.
    dry_run:
        Resolve destinations but do not touch disk.

    Returns
    -------
    list[PhotoRecord]
        Records with ``destination`` fields populated.
    """
    root = Path(root_dir).expanduser().absolute()
    target = Path(output_dir).expanduser().absolute() if output_dir else root
    target_root = str(target)

    records = discover_photos(str(root))
    clusters = cluster_records(records, config_obj)

    moved = skipped = 0
    for start, end, recs in clusters:
        folder_name = _folder_name(start, end)
        dest_dir = str(target / folder_name)
        if not dry_run:
            Path(dest_dir).mkdir(parents=True, exist_ok=True)

        for rec in recs:
            rec.destination = _resolve_unique_destination(dest_dir, rec.name)
            if Path(rec.path).resolve() == Path(rec.destination).resolve():
                skipped += 1
                continue
            if dry_run:
                logger.info("[dry-run] would move %s -> %s", rec.path, rec.destination)
                continue
            try:
                journal.safe_move(
                    target_root, rec.path, rec.destination,
                    op="organize", restore=rec.path, event=folder_name,
                )
                moved += 1
            except (OSError, ValueError) as exc:
                logger.error("Failed to move %s: %s", rec.path, exc)

    logger.info(
        "Stage 1 complete: %d moved, %d in-place, %d clusters (dry_run=%s)",
        moved, skipped, len(clusters), dry_run,
    )
    return records


def summarize_clusters(
    records: List[PhotoRecord],
    config_obj: Optional[dict] = None,
) -> str:
    """Human-readable cluster summary for CLI output."""
    lines = []
    for start, end, recs in cluster_records(records, config_obj):
        with_gps = sum(1 for r in recs if r.gps is not None)
        lines.append(
            f"{_folder_name(start, end)}: {len(recs)} photos "
            f"({with_gps} with GPS)"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python organizer.py <root_dir> [output_dir]")
        sys.exit(1)
    recs = organize_directory(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(summarize_clusters(recs))
