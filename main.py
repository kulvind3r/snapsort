"""
main.py — SnapSort entry point: two-stage workflow orchestration

Stage 1 (headless): prompt for a source directory (or take it from the CLI),
run ``organizer.organize_directory`` to restructure photos into per-event /
per-trip folders.

Stage 2 (interactive): launch ``ui.PhotoCullerUI`` on the organized folder
for keyboard-driven duplicate culling.

Usage:
    python main.py [source_dir] [--output DIR] [--dry-run] [--skip-organize]

    python main.py                      # interactive prompts
    python main.py ~/Pictures           # source from CLI
    python main.py ~/Pictures --dry-run # organize only, preview moves
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import config as snapsort_config
import logging_setup
from organizer import organize_directory, summarize_clusters, discover_photos
from ui import run_culler, pick_folder_and_run

logger = logging.getLogger("snapsort.main")


def _setup_logging(verbose: bool) -> None:
    # Rotating file handler + startup banner + excepthook (review Section 6).
    logging_setup.setup_logging(verbose=verbose)

    # Review 3.7: honour [logging] level from snapsort.toml (verbose CLI
    # flag still forces DEBUG).
    level_name = snapsort_config.logging_level(snapsort_config.load_config())
    if not verbose:
        level = getattr(logging, level_name, logging.INFO)
    else:
        level = logging.DEBUG
    logging.getLogger().setLevel(level)
    logger.debug("Effective log level: %s",
                 logging.getLogger().getLevelName())


def _validate_dir(path: str, prompt: str) -> str:
    """Normalize and confirm a directory exists."""
    p = Path(path).expanduser().absolute()
    if not p.is_dir():
        raise FileNotFoundError(f"{prompt} is not a directory: {p}")
    return str(p)


def _has_console() -> bool:
    """Return True when a real stdin terminal is available.

    PyInstaller GUI builds (console=False) set sys.stdin to None.
    Calling input() in that state raises OSError: 'lost sys.stdin'.
    """
    return sys.stdin is not None and not getattr(sys, "frozen", False)


def _confirm(message: str) -> bool:
    """Ask a yes/no question; use Tkinter messagebox when stdin is absent."""
    if _has_console():
        try:
            return input(message).strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False
    # Frozen / no-console path (Windows .exe with console=False).
    import tkinter as _tk
    from tkinter import messagebox as _mb
    root = _tk.Tk()
    root.withdraw()
    root.update()
    result = _mb.askyesno("SnapSort", message.rstrip(" ").rstrip("[y/N]").rstrip("[Y/n]").strip())
    root.destroy()
    return result


def _run_stage1(source_dir: str, output_dir: str | None,
                dry_run: bool, config_obj: dict | None = None) -> str:
    """Execute the headless organizing stage; return the target root."""
    if output_dir:
        target = Path(output_dir).expanduser().absolute()
    else:
        target = Path(source_dir).expanduser().absolute()
    target_root = str(target)

    logger.info("Stage 1 starting — source: %s  target: %s  dry_run: %s",
                source_dir, target_root, dry_run)
    if _has_console():
        banner = "=" * 60
        print(f"\n{banner}\n  STAGE 1 — Headless Organization\n{banner}")
        print(f"  Source : {source_dir}")
        print(f"  Target : {target_root}")
        if dry_run:
            print("  Mode   : DRY RUN (no files will be moved)")

    records = organize_directory(source_dir, target_root, dry_run=dry_run,
                                 config_obj=config_obj)

    by_date = {r.date for r in records if r.date is not None}
    with_gps = sum(1 for r in records if r.gps is not None)
    logger.info(
        "Stage 1 complete — %d files, %d distinct dates, %d with GPS",
        len(records), len(by_date), with_gps,
    )
    if _has_console():
        print(f"\n  Files discovered : {len(records)}")
        print(f"  Distinct dates   : {len(by_date)}")
        print(f"  Photos w/ GPS    : {with_gps}")
        print("\n  Cluster summary:")
        for line in summarize_clusters(records, config_obj).splitlines():
            print(f"    {line}")
        print("=" * 60 + "\n")
    return target_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="snapsort",
        description="Local auto-organizing photo culler "
                    "(Stage 1: organize, Stage 2: cull).",
    )
    parser.add_argument(
        "source", nargs="?", default=None,
        help="Source directory containing photos.",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Optional separate output root for organized folders.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Resolve moves without touching disk (Stage 1 only).",
    )
    parser.add_argument(
        "--skip-organize", action="store_true",
        help="Skip Stage 1; go straight to the culling UI.",
    )
    parser.add_argument(
        "--skip-cull", action="store_true",
        help="Run Stage 1 only; do not launch the culling UI.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args(argv)
    # Review 6: set up logging as the very first action, before any other
    # side effects.
    log_file = logging_setup.setup_logging(verbose=args.verbose)

    # ---- Resolve source directory ---------------------------------------
    source_dir = args.source
    if source_dir is None:
        if args.skip_organize:
            print("Selecting organized folder to cull...")
            pick_folder_and_run()
            return 0
        if _has_console():
            try:
                source_dir = input("Enter source photos directory: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return 1
        else:
            # Frozen GUI build: use a folder picker dialog.
            import tkinter as _tk
            from tkinter import filedialog as _fd
            root = _tk.Tk()
            root.withdraw()
            root.update()
            source_dir = _fd.askdirectory(
                title="Select source photos folder",
                initialdir=str(Path.home()),
                parent=root,
            )
            root.destroy()
            if not source_dir:
                logger.info("User cancelled source folder selection.")
                return 0
    if not source_dir:
        print("No source directory provided. Aborting.")
        return 1

    try:
        source_dir = _validate_dir(source_dir, "Source")
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    # Review 3.7: resolve snapsort.toml (library-local > user-global >
    # defaults) once, before either stage.
    config_obj = snapsort_config.load_config(source_dir)

    # Guard: refuse to organize a folder that has no photos at all.
    preview = discover_photos(source_dir)
    if not preview and not args.skip_organize:
        print("No supported photo files found in the source directory.")
        return 0

    # ---- Stage 1 ---------------------------------------------------------
    if not args.skip_organize:
        if args.dry_run:
            target_root = _run_stage1(source_dir, args.output, dry_run=True,
                                      config_obj=config_obj)
            print("Dry run complete. Re-run without --dry-run to apply.")
            return 0

        if not _confirm(
            "Stage 1 will MOVE files on disk. Continue? [y/N] "
        ):
            print("Stage 1 cancelled.")
            return 0
        target_root = _run_stage1(source_dir, args.output, dry_run=False,
                                  config_obj=config_obj)

        if args.skip_cull:
            print("Stage 2 skipped (--skip-cull).")
            return 0

        if not _confirm(
            "Launch the interactive culling UI now? [Y/n] "
        ):
            print("Culling UI skipped.")
            return 0
    else:
        # Cull-only path: use source (assumed already organized) as target.
        target_root = source_dir

    # ---- Stage 2 ---------------------------------------------------------
    print("\nLaunching culling UI on:\n  " + target_root + "\n")
    try:
        run_culler(target_root)
    except Exception:
        logger.exception("Culling UI crashed")
        return 1
    print("SnapSort finished.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)