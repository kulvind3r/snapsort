"""
logging_setup.py — Structured file-based logging (review Section 6)

Configures a ``snapsort`` root logger with:

* a rotating file handler (5 MB per file, 3 backups) writing to the
  platform-appropriate user log directory — *not* next to the executable,
  which may be in a non-writable location when frozen;
* an optional console handler (always on in dev; off when frozen unless
  a console was kept);
* a structured startup banner (version, platform, Python, ``sys.frozen``);
* a ``sys.excepthook`` that logs any uncaught exception at CRITICAL with
  its full traceback before the window closes.

Call :func:`setup_logging` as the very first action in ``main()``.
"""

from __future__ import annotations

import logging
import logging.handlers
import platform
import sys
from pathlib import Path
from typing import Optional

__version__ = "0.1.0"

_CONFIGURED = False


def get_log_path() -> Path:
    """Return the directory where snapsort.log is written.

    When running as a frozen binary the log is placed **next to the
    executable** so the user can find it immediately without hunting through
    platform-specific app-data folders.  If that location is not writable
    (e.g. the binary lives in Program Files) we fall back to the
    platform-appropriate user log directory.
    """
    if getattr(sys, "frozen", False):
        # Preferred: same folder as the .exe — visible right next to it.
        exe_dir = Path(sys.executable).parent
        try:
            exe_dir.mkdir(parents=True, exist_ok=True)
            probe = exe_dir / ".snapsort_log_probe"
            probe.touch()
            probe.unlink()
            return exe_dir
        except OSError:
            pass  # fall through to platform default

    # Dev mode or non-writable binary location → platform log directory.
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Local" / "SnapSort" / "Logs"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Logs" / "SnapSort"
    else:
        base = Path.home() / ".local" / "share" / "snapsort" / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _install_excepthook(logger: logging.Logger) -> None:
    """Log uncaught exceptions at CRITICAL with full traceback (6.5)."""

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            # Respect Ctrl-C: default behaviour, no CRITICAL spam.
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
        )

    sys.excepthook = _hook


def _log_startup_banner(logger: logging.Logger, log_file: Path) -> None:
    """Structured, self-identifying startup banner."""
    logger.info("=" * 60)
    logger.info("SnapSort starting up")
    logger.info("  Version   : %s", __version__)
    logger.info("  Platform  : %s %s", sys.platform, platform.version())
    logger.info("  Python    : %s", sys.version.split()[0])
    logger.info("  Frozen    : %s", getattr(sys, "frozen", False))
    logger.info("  Executable: %s", sys.executable)
    logger.info("  Log file  : %s", log_file)
    logger.info("=" * 60)


def setup_logging(verbose: bool = False,
                  console: Optional[bool] = None,
                  log_file: Optional[Path] = None) -> Path:
    """Configure the ``snapsort`` logger. Idempotent.

    Returns the path of the log file (for display in the UI status bar).
    """
    global _CONFIGURED
    if _CONFIGURED:
        return log_file or (get_log_path() / "snapsort.log")

    logger = logging.getLogger("snapsort")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler: 5 MB cap, 3 backups (6.2).
    log_file = log_file or (get_log_path() / "snapsort.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Console handler: on in dev; suppressed in frozen console-less builds.
    if console is None:
        console = not getattr(sys, "frozen", False)
    if console:
        stream = logging.StreamHandler()
        stream.setLevel(logging.DEBUG if verbose else logging.INFO)
        stream.setFormatter(fmt)
        logger.addHandler(stream)

    _install_excepthook(logger)
    _log_startup_banner(logger, log_file)
    _CONFIGURED = True
    return log_file


def get_log_file() -> Path:
    """Best-effort current log file path (for the UI status bar)."""
    return get_log_path() / "snapsort.log"