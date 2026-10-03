"""
quality.py — Per-image quality metrics: sharpness, tilt, and data models.

Responsibilities:
  * ImageMetrics and SimilarityCluster data models.
  * OpenCV image loading and memory helpers.
  * Sharpness scoring via variance of the Laplacian.
  * Horizon/axis tilt scoring via Canny + probabilistic Hough lines.

All OpenCV/NumPy buffers are released explicitly (del + gc) to prevent
native memory accumulation during long batch scans.
"""

from __future__ import annotations

import gc
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import cv2

logger = logging.getLogger("snapsort.quality")

ANALYZE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif",
}
_METRIC_MAX_SIDE = 1024
_HOUGH_MIN_LINE_FRAC = 0.15
_ANGLE_TOLERANCE_DEG = 15.0
IMAGES_PER_CLUSTER_MAX = 3


@dataclass
class ImageMetrics:
    """Per-image quality analysis result."""

    path: str
    phash: Optional[str] = None
    sharpness: Optional[float] = None
    tilt_score: Optional[float] = None
    dominant_axis: Optional[str] = None
    line_count: int = 0

    @property
    def analyzable(self) -> bool:
        return self.sharpness is not None and self.tilt_score is not None

    def recommendation_rank(self) -> float:
        """Lower is better: sharpness dominates, tilt is a tie-breaker."""
        if not self.analyzable:
            return float("inf")
        return -self.sharpness + (self.tilt_score / 15.0)


@dataclass
class SimilarityCluster:
    """A group of visually similar images within one folder."""

    folder: str
    members: List[ImageMetrics] = field(default_factory=list)

    def best_pick(self) -> Optional[ImageMetrics]:
        analyzable = [m for m in self.members if m.analyzable]
        if not analyzable:
            return self.members[0] if self.members else None
        return min(analyzable, key=ImageMetrics.recommendation_rank)

    def sorted_members(self) -> List[ImageMetrics]:
        """Best pick first, then rest by rank, capped at display limit."""
        best = self.best_pick()
        rest = sorted(
            [m for m in self.members if m is not best],
            key=ImageMetrics.recommendation_rank,
        )
        return ([best] if best else []) + rest[:IMAGES_PER_CLUSTER_MAX - (1 if best else 0)]


LOW_SHARPNESS_THRESHOLD = 80.0
"""Laplacian variance below this flags an image as blurry or shaky.
Tune via ``snapsort.toml`` ``[quality] sharpness_threshold``."""


@dataclass
class QualityReviewItem:
    """A low-quality photo (blurry/shaky) flagged for review with optional sharper alternatives."""

    folder: str
    bad: ImageMetrics
    alternatives: List[ImageMetrics] = field(default_factory=list)
    reason: str = "blurry"

    def best_alternative(self) -> Optional[ImageMetrics]:
        """Return the sharpest available alternative, or None."""
        return self.alternatives[0] if self.alternatives else None


def _is_analyzable_file(path: str) -> bool:
    return Path(path).suffix.lower() in ANALYZE_EXTENSIONS


def _load_cv(path: str) -> Optional[np.ndarray]:
    """Load and optionally downscale an image via OpenCV."""
    p = Path(path)
    if not _is_analyzable_file(path) or not p.is_file():
        return None
    img = cv2.imread(str(p), cv2.IMREAD_COLOR)
    if img is None:
        logger.debug("cv2.imread failed for %s", path)
        return None
    h, w = img.shape[:2]
    if max(h, w) > _METRIC_MAX_SIDE:
        scale = _METRIC_MAX_SIDE / float(max(h, w))
        img = cv2.resize(
            img,
            (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return img


def _compute_gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def _dispose(*arrays) -> None:
    """Release native array references and nudge the GC."""
    for a in arrays:
        if a is not None:
            del a
    gc.collect()


def get_sharpness_score(image_path: str) -> Optional[float]:
    """Return Laplacian variance (higher = sharper), or None on failure."""
    img = gray = lap = None
    try:
        img = _load_cv(image_path)
        if img is None:
            return None
        gray = _compute_gray(img)
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        return float(lap.var())
    except cv2.error:
        logger.warning("OpenCV error computing sharpness for %s", image_path)
        return None
    except Exception:
        logger.exception("Unexpected error computing sharpness for %s", image_path)
        return None
    finally:
        _dispose(img, gray, lap)


def _segment_angle_deg(x1: float, y1: float, x2: float, y2: float) -> float:
    """Segment angle in [0, 90): 0 = horizontal, 90 = vertical."""
    angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
    if angle >= 90.0:
        angle = 180.0 - angle
    return min(90.0, angle)


def _deviation_from_axis(angle: float) -> float:
    """Deviation in degrees from the nearest pure axis (0° or 90°)."""
    return angle if angle <= 45.0 else abs(90.0 - angle)


def get_tilt_angle(image_path: str) -> Optional[Tuple[float, str, int]]:
    """Return (mean_deviation_deg, dominant_axis, line_count) or None."""
    img = gray = edges = None
    try:
        img = _load_cv(image_path)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        min_line_len = max(8, int(min(h, w) * _HOUGH_MIN_LINE_FRAC))
        lines = cv2.HoughLinesP(
            edges, rho=1, theta=np.pi / 180.0,
            threshold=20, minLineLength=min_line_len, maxLineGap=5,
        )
        if lines is None or len(lines) == 0:
            return 0.0, None, 0

        dev_horiz: List[float] = []
        dev_vert: List[float] = []
        for seg in lines:
            x1, y1, x2, y2 = seg[0]
            angle = _segment_angle_deg(float(x1), float(y1), float(x2), float(y2))
            dev = _deviation_from_axis(angle)
            if dev > _ANGLE_TOLERANCE_DEG:
                continue
            (dev_horiz if angle <= 45.0 else dev_vert).append(dev)

        total = len(dev_horiz) + len(dev_vert)
        if total == 0:
            return 0.0, None, len(lines)

        h_mean = sum(dev_horiz) / len(dev_horiz) if dev_horiz else 0.0
        v_mean = sum(dev_vert) / len(dev_vert) if dev_vert else 0.0
        mean_dev = (h_mean + v_mean) / (2 if dev_horiz and dev_vert else 1)
        dominant = "horizontal" if len(dev_horiz) >= len(dev_vert) else "vertical"
        return float(mean_dev), dominant, total
    except cv2.error:
        logger.warning("OpenCV error computing tilt for %s", image_path)
        return None
    except Exception:
        logger.exception("Unexpected error computing tilt for %s", image_path)
        return None
    finally:
        _dispose(img, gray, edges)
