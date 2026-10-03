"""
config.py — User-tunable settings (review 3.7)

Tuning constants that previously lived as module-level literals in
``analyzer`` and ``organizer`` can now be overridden without editing
source code. Values are resolved in this order (first wins):

1. ``snapsort.toml`` next to the photos being processed (library-local)
2. ``~/.config/snapsort/config.toml`` (user-global)
3. Built-in defaults (``DEFAULTS``)

``python3 -m snapsort.main`` (CLI) and the culling GUI both call
``load_config(root)`` at startup. A missing or malformed file logs a
warning and falls back to defaults — the app never refuses to start
because of a config problem.

Example ``snapsort.toml``::

    [clustering]
    hamming_threshold = 12      # 0..256 (16x16 hash)

    [trip]
    max_centroid_distance_km = 80.0
    max_trip_span_days = 10

    [logging]
    level = "DEBUG"
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional

logger = logging.getLogger("snapsort.config")

try:  # Python 3.11+ ships tomllib; 3.9/3.10 use the MIT tomli shim.
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    try:
        import tomli as _toml
    except ModuleNotFoundError:
        _toml = None


# ---------------------------------------------------------------------------
# Built-in defaults (mirrors the previous module-level constants)
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "clustering": {
        # pHash Hamming distance at/under which two images are "similar".
        "hamming_threshold": 10,
        # 16x16 -> 256-bit DCT hash for finer detail.
        "phash_size": 16,
        # UI displays up to N side-by-side panels.
        "max_images_per_cluster": 3,
    },
    "trip": {
        # Max gap (days) between daily groups merged into one trip.
        "max_date_gap_days": 1,
        # Continuous-day span considered a trip without GPS.
        "max_trip_span_days": 7,
        # Minimum days for a trip cluster.
        "min_trip_span_days": 2,
        # Max centroid distance (km) to merge GPS-tagged days.
        "max_centroid_distance_km": 50.0,
    },
    "logging": {
        # Root logger level name: DEBUG / INFO / WARNING / ERROR.
        "level": "INFO",
    },
}

CONFIG_FILENAME = "snapsort.toml"


def _user_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(__import__("os").environ.get(
            "LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "SnapSort" / "config.toml"
    if sys.platform == "darwin":
        return Path.home() / ".config" / "snapsort" / "config.toml"
    return Path(
        __import__("os").environ.get(
            "XDG_CONFIG_HOME", str(Path.home() / ".config"))
    ) / "snapsort" / "config.toml"


def _deep_merge(base: Dict[str, Dict[str, Any]],
                override: Dict[str, Dict[str, Any]]
                ) -> Dict[str, Dict[str, Any]]:
    """Return a copy of ``base`` with ``override`` merged in (section by
    section, key by key). ``base`` is not mutated."""
    merged: Dict[str, Dict[str, Any]] = {
        k: dict(v) for k, v in base.items()
    }
    for section, values in override.items():
        if not isinstance(values, dict):
            continue
        merged.setdefault(section, {}).update(values)
    return merged


def _read_toml(path: Path) -> Optional[Dict[str, Dict[str, Any]]]:
    if _toml is None:
        logger.warning("No TOML parser available (need Python 3.11+ or "
                       "'tomli' on 3.9/3.10); ignoring %s", path)
        return None
    try:
        with open(path, "rb") as fh:
            data = _toml.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, _toml.TOMLDecodeError) as exc:
        logger.warning("Could not read config %s: %s — using defaults",
                       path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("Config %s is malformed (top-level table expected) "
                       "— using defaults", path)
        return None
    return data


def load_config(root: Optional[str] = None
                ) -> Dict[str, Dict[str, Any]]:
    """Resolve the effective configuration.

    Precedence (first wins): ``snapsort.toml`` in ``root`` (if given) >
    user-global config > built-in defaults.
    """
    config: Dict[str, Dict[str, Any]] = _deep_merge(DEFAULTS, {})
    user_file = _user_config_path()
    user_data = _read_toml(user_file)
    if user_data is not None:
        config = _deep_merge(config, user_data)
        logger.info("Loaded user config: %s", user_file)
    if root is not None:
        local_file = Path(root) / CONFIG_FILENAME
        local_data = _read_toml(local_file)
        if local_data is not None:
            config = _deep_merge(config, local_data)
            logger.info("Loaded library config: %s", local_file)
    return config


# ---------------------------------------------------------------------------
# Typed accessors (fallback to defaults if a key is missing)
# ---------------------------------------------------------------------------

def get(config: Dict[str, Dict[str, Any]], section: str, key: str) -> Any:
    value = config.get(section, {}).get(key)
    if value is None:
        value = DEFAULTS[section][key]
    return value


def clustering(config: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return dict(DEFAULTS["clustering"], **config.get("clustering", {}))


def trip(config: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    return dict(DEFAULTS["trip"], **config.get("trip", {}))


def logging_level(config: Dict[str, Dict[str, Any]]) -> str:
    level = str(get(config, "logging", "level")).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        logger.warning("Unknown logging level %r; falling back to INFO",
                       level)
        return "INFO"
    return level