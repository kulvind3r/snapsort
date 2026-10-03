"""
tests/unit/test_quality.py — Unit tests for quality.py image metrics.

Covers:
  * get_tilt_angle — both HoughLinesP output shapes (the N,1,4 vs N,4 regression).
  * get_sharpness_score — returns a float for a valid image, None for a bad path.
  * ImageMetrics.recommendation_rank — ordering behaviour.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from quality import (
    ImageMetrics,
    get_sharpness_score,
    get_tilt_angle,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def real_jpeg(tmp_path):
    """A minimal valid JPEG written by Pillow — no EXIF required."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (320, 240), (180, 120, 60))
    draw = ImageDraw.Draw(img)
    # Horizontal and vertical lines → Hough should find axis-aligned segments
    draw.line([(20, 60), (300, 60)], fill=(0, 0, 0), width=3)
    draw.line([(160, 20), (160, 220)], fill=(0, 0, 0), width=3)
    p = tmp_path / "test_real.jpg"
    img.save(str(p), "JPEG")
    return str(p)


# ---------------------------------------------------------------------------
# get_tilt_angle — shape regression tests
# ---------------------------------------------------------------------------

def _make_lines(shape: str) -> np.ndarray:
    """Return a synthetic HoughLinesP result in the requested shape.

    'old' → (N, 1, 4) as returned by older OpenCV builds
    'new' → (N, 4)    as returned by newer OpenCV 4.x builds
    """
    raw = [[10, 20, 100, 20], [30, 10, 30, 90]]  # horizontal + vertical
    if shape == "old":
        return np.array([[seg] for seg in raw], dtype=np.int32)  # (2, 1, 4)
    return np.array(raw, dtype=np.int32)                         # (2, 4)


def _patch_hough(lines_array):
    """Context manager: patches HoughLinesP to return *lines_array*."""
    return patch("quality.cv2.HoughLinesP", return_value=lines_array)


def _patch_cv_load(real_jpeg):
    """Return a tiny synthetic BGR image via _load_cv so we avoid file I/O."""
    gray = np.full((100, 100), 128, dtype=np.uint8)
    bgr  = cv2_bgr_from_gray(gray)
    return patch("quality._load_cv", return_value=bgr)


def cv2_bgr_from_gray(gray: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


@pytest.mark.parametrize("hough_shape", ["old", "new"])
def test_get_tilt_angle_both_hough_shapes(hough_shape, tmp_path):
    """get_tilt_angle must not raise for either HoughLinesP output shape.

    Regression: prior to fix, shape (N, 4) caused
    'TypeError: cannot unpack non-iterable numpy.int32 object' at seg[0].
    """
    # Arrange
    import cv2
    fake_bgr = np.full((100, 100, 3), 128, dtype=np.uint8)
    lines = _make_lines(hough_shape)

    with patch("quality._load_cv", return_value=fake_bgr), \
         patch("quality.cv2.HoughLinesP", return_value=lines):

        # Act
        result = get_tilt_angle("fake_path.jpg")

    # Assert — must return a 3-tuple, not raise
    assert result is not None, f"Returned None for hough_shape={hough_shape!r}"
    mean_dev, axis, count = result
    assert isinstance(mean_dev, float)
    assert count >= 0


def test_get_tilt_angle_no_lines_returns_zero():
    """When HoughLinesP finds nothing, return (0.0, None, 0) not None."""
    import cv2
    fake_bgr = np.full((100, 100, 3), 128, dtype=np.uint8)

    with patch("quality._load_cv", return_value=fake_bgr), \
         patch("quality.cv2.HoughLinesP", return_value=None):

        result = get_tilt_angle("fake_path.jpg")

    assert result == (0.0, None, 0)


def test_get_tilt_angle_bad_path_returns_none():
    """Non-existent file must return None without raising."""
    result = get_tilt_angle("/nonexistent/path/image.jpg")
    assert result is None


# ---------------------------------------------------------------------------
# get_sharpness_score
# ---------------------------------------------------------------------------

def test_get_sharpness_score_valid_image_returns_float(real_jpeg):
    """Sharp image returns a positive float."""
    score = get_sharpness_score(real_jpeg)
    assert score is not None
    assert isinstance(score, float)
    assert score > 0.0


def test_get_sharpness_score_bad_path_returns_none():
    """Missing file returns None, does not raise."""
    assert get_sharpness_score("/no/such/file.jpg") is None


def test_get_sharpness_score_blurry_less_than_sharp(tmp_path):
    """Blurred image must score lower than its sharp original."""
    from PIL import Image, ImageFilter
    img = Image.new("RGB", (320, 240), (200, 100, 50))
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for i in range(20):
        d.line([(i * 15, 0), (i * 15, 240)], fill=(0, 0, 0), width=2)

    sharp_p = str(tmp_path / "sharp.jpg")
    blurry_p = str(tmp_path / "blurry.jpg")
    img.save(sharp_p, "JPEG")
    img.filter(ImageFilter.GaussianBlur(8)).save(blurry_p, "JPEG")

    sharp_score  = get_sharpness_score(sharp_p)
    blurry_score = get_sharpness_score(blurry_p)

    assert sharp_score is not None and blurry_score is not None
    assert blurry_score < sharp_score, (
        f"Expected blurry ({blurry_score:.1f}) < sharp ({sharp_score:.1f})"
    )


# ---------------------------------------------------------------------------
# ImageMetrics.recommendation_rank
# ---------------------------------------------------------------------------

def test_recommendation_rank_sharpness_dominates():
    """Higher sharpness → lower rank (better pick)."""
    sharp  = ImageMetrics(path="s.jpg", sharpness=500.0, tilt_score=0.0)
    blurry = ImageMetrics(path="b.jpg", sharpness=20.0,  tilt_score=0.0)
    assert sharp.recommendation_rank() < blurry.recommendation_rank()


def test_recommendation_rank_non_analyzable_is_worst():
    """Photo with no metrics sorts last (rank = inf)."""
    no_data = ImageMetrics(path="x.jpg")
    decent  = ImageMetrics(path="y.jpg", sharpness=100.0, tilt_score=1.0)
    assert no_data.recommendation_rank() == float("inf")
    assert decent.recommendation_rank() < no_data.recommendation_rank()
