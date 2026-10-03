"""
metadata.py — Photo file discovery and EXIF/GPS metadata extraction.

Responsibilities:
  * PhotoRecord data model.
  * Recursively discover supported photo files under a directory.
  * Extract capture timestamps: EXIF → filename regex → OS mtime.
  * Extract GPS coordinates as decimal degrees.
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ExifTags

import paths

logger = logging.getLogger("snapsort.metadata")

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    logger.warning(
        "pillow-heif not installed; HEIC/HEIF files will be discovered "
        "but not analyzed. Install with: pip install pillow-heif"
    )

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}

_FILENAME_DATE_RE = re.compile(
    r"(19|20)\d{2}[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])"
)
_TIME_TAGS = ("DateTimeOriginal", "DateTimeDigitized", "DateTime")
_DATE_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d",
    "%Y-%m-%d",
)


@dataclass
class PhotoRecord:
    """Metadata for a single discovered photo file."""

    path: str
    name: str
    ext: str
    date: Optional[date] = None
    full_timestamp: Optional[datetime] = None
    gps: Optional[Tuple[float, float]] = None  # (lat, lon) decimal degrees
    source: str = ""        # 'exif' | 'filename' | 'mtime'
    destination: str = ""   # resolved move target path

    @property
    def day_key(self) -> Optional[date]:
        return self.date


def discover_photos(root_dir: str) -> List[PhotoRecord]:
    """Recursively find all supported photo files under ``root_dir``.

    Prunes ``_Discarded`` and ``_Rejects`` so re-runs never reprocess
    previously culled photos.
    """
    root = Path(root_dir).expanduser()
    if not root.is_dir():
        logger.warning("Root directory does not exist: %s", root_dir)
        return []

    records: List[PhotoRecord] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in paths.PRUNED_DIRNAMES]
        for filename in filenames:
            full = Path(dirpath) / filename
            ext = full.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS or not full.is_file():
                continue
            record = PhotoRecord(path=str(full), name=full.name, ext=ext)
            try:
                _populate_metadata(record)
            except Exception:
                logger.exception("Failed to read metadata for %s", full)
                record.date = _safe_mtime_date(str(full))
                record.source = "mtime"
            records.append(record)

    logger.info("Discovered %d photo files under %s", len(records), root_dir)
    return records


def _populate_metadata(record: PhotoRecord) -> None:
    """Fill in date + GPS for a single record."""
    raster = record.ext in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
    if raster:
        try:
            with Image.open(record.path) as img:
                img.load()
                exif = img.getexif() if hasattr(img, "getexif") else {}
                record.date, record.full_timestamp = _extract_exif_datetime(exif)
                if record.date is not None:
                    record.source = "exif"
                record.gps = _extract_gps(exif)
        except Exception:
            logger.debug("EXIF read failed for %s", record.path, exc_info=True)

    if record.date is None:
        m = _FILENAME_DATE_RE.search(record.name)
        if m:
            d = _parse_filename_date(m)
            if d is not None:
                record.date = d
                record.source = "filename"

    if record.date is None:
        record.date = _safe_mtime_date(record.path)
        record.source = "mtime"


def _extract_exif_datetime(
    exif: dict,
) -> Tuple[Optional[date], Optional[datetime]]:
    """Return (date, datetime) from a Pillow getexif() mapping."""
    name_to_id = {v: k for k, v in ExifTags.TAGS.items()}
    tags = (
        {ExifTags.TAGS.get(k, f"tag_{k}"): v for k, v in exif.items()}
        if isinstance(exif, dict) else {}
    )
    for tag_name in _TIME_TAGS:
        raw = None
        tag_id = name_to_id.get(tag_name)
        if tag_id is not None and tag_id in exif:
            raw = exif[tag_id]
        else:
            raw = tags.get(tag_name)
        if not raw:
            continue
        for fmt in _DATE_FORMATS:
            try:
                dt = datetime.strptime(str(raw).strip(), fmt)
                return dt.date(), dt
            except ValueError:
                continue
    return None, None


def _parse_filename_date(match: re.Match) -> Optional[date]:
    """Build a validated date from a filename regex match."""
    digits = re.sub(r"[-_]", "", match.group(0))
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        logger.debug("Filename date %r is not a real calendar date", match.group(0))
        return None


def _safe_mtime_date(file_path: str) -> Optional[date]:
    """Return the OS modification date of a file, or None on failure."""
    try:
        return datetime.fromtimestamp(Path(file_path).stat().st_mtime).date()
    except OSError:
        logger.debug("Could not stat %s", file_path, exc_info=True)
        return None


def get_image_date(file_path: str) -> Optional[date]:
    """Public helper: resolve a capture date for a single file."""
    p = Path(file_path)
    rec = PhotoRecord(path=file_path, name=p.name, ext=p.suffix.lower())
    try:
        _populate_metadata(rec)
    except Exception:
        rec.date = _safe_mtime_date(file_path)
    return rec.date


def _dms_to_decimal(value) -> Optional[float]:
    """Convert EXIF GPS DMS (tuple of fractions) to decimal degrees."""
    try:
        if isinstance(value, (tuple, list)):
            d, m, s = (float(x) for x in value[:3])
        else:
            d, m, s = float(value), 0.0, 0.0
        return d + m / 60.0 + s / 3600.0
    except (TypeError, ValueError, IndexError):
        return None


def _extract_gps(exif) -> Optional[Tuple[float, float]]:
    """Extract (lat, lon) decimal degrees from a Pillow Exif object."""
    gps_ifd: dict = {}
    if hasattr(exif, "get_ifd"):
        try:
            gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
        except Exception:
            gps_ifd = {}
        if not gps_ifd:
            try:
                gps_ifd = exif.get_ifd("GPS")
            except Exception:
                gps_ifd = {}
    if not gps_ifd:
        try:
            merged = exif._get_merged_dict() if hasattr(exif, "_get_merged_dict") else {}
            val = merged.get(34853) or merged.get(ExifTags.IFD.GPSInfo)
            if isinstance(val, dict):
                gps_ifd = val
        except Exception:
            pass
    if not gps_ifd and isinstance(exif, dict):
        val = exif.get(34853) or exif.get(ExifTags.IFD.GPSInfo)
        if isinstance(val, dict):
            gps_ifd = val
    if not gps_ifd:
        return None

    def _get(tag_name: str):
        tag_id = ExifTags.GPSTAG.get(tag_name)
        return gps_ifd.get(tag_id) if tag_id is not None else None

    lat_val = _dms_to_decimal(_get("GPSLatitude"))
    lon_val = _dms_to_decimal(_get("GPSLongitude"))
    if lat_val is None or lon_val is None:
        return None
    if str(_get("GPSLatitudeRef") or "").strip().upper() == "S":
        lat_val = -lat_val
    if str(_get("GPSLongitudeRef") or "").strip().upper() == "W":
        lon_val = -lon_val
    if abs(lat_val) > 90.0 or abs(lon_val) > 180.0:
        return None
    return (lat_val, lon_val)


def get_image_gps(file_path: str) -> Optional[Tuple[float, float]]:
    """Public helper: extract (lat, lon) decimal degrees from a single file."""
    if Path(file_path).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None
    try:
        with Image.open(file_path) as img:
            img.load()
            return _extract_gps(img.getexif())
    except Exception:
        logger.debug("GPS read failed for %s", file_path, exc_info=True)
        return None
