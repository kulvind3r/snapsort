"""
analyzer.py — Perceptual hashing, similarity clustering, and directory analysis.

Delegates image quality metrics (sharpness, tilt, data models) to ``quality``.
This module owns pHash computation, union-find clustering, and the top-level
``analyze_directory`` entry point consumed by ``ui``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from analysis_cache import AnalysisCache
from paths import PRUNED_DIRNAMES
from quality import (  # re-exported for callers that import from analyzer
    ImageMetrics,
    SimilarityCluster,
    ANALYZE_EXTENSIONS,
    IMAGES_PER_CLUSTER_MAX,
    _is_analyzable_file,
    get_sharpness_score,
    get_tilt_angle,
    LOW_SHARPNESS_THRESHOLD,
    QualityReviewItem,
)

logger = logging.getLogger("snapsort.analyzer")

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    logger.warning(
        "pillow-heif not installed; HEIC/HEIF photos will not be analyzable."
    )

try:
    import imagehash
    from PIL import Image
    _HASH_BACKEND = "imagehash"
except ImportError:
    Image = None
    _HASH_BACKEND = "none"

try:
    import cv2
    import numpy as np
    from quality import _load_cv, _compute_gray, _dispose
    _CV_AVAILABLE = True
except ImportError:
    _CV_AVAILABLE = False

HASH_HAMMING_THRESHOLD = 10
PHASH_SIZE = 16


def get_phash_hex(image_path: str) -> Optional[str]:
    """Return a perceptual hash hex string, or None on failure."""
    if _HASH_BACKEND == "imagehash" and Image is not None:
        try:
            with Image.open(image_path) as img:
                img.load()
                return str(imagehash.phash(img, hash_size=PHASH_SIZE))
        except Exception:
            logger.debug("imagehash failed for %s", image_path, exc_info=True)
            return None

    if not _CV_AVAILABLE:
        return None
    img = gray = None
    try:
        img = _load_cv(image_path)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (PHASH_SIZE, PHASH_SIZE))
        dct = cv2.dct(np.float32(gray))
        low = dct[:PHASH_SIZE // 2, :PHASH_SIZE // 2]
        avg = float(low.mean())
        return "".join("1" if b else "0" for b in (low > avg).flatten())
    except Exception:
        logger.debug("fallback hash failed for %s", image_path, exc_info=True)
        return None
    finally:
        _dispose(img, gray)


def _hamming(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """Hamming distance between two hash strings."""
    if a is None or b is None:
        return None
    if len(a) != len(b):
        return 999
    if set(a) <= {"0", "1"} and set(b) <= {"0", "1"} and len(a) > 64:
        return sum(x != y for x, y in zip(a, b))
    try:
        n = int(a, 16) ^ int(b, 16)
        count = 0
        while n:
            count += n & 1
            n >>= 1
        return count
    except ValueError:
        return sum(x != y for x, y in zip(a, b))


def analyze_image(
    image_path: str,
    cache: Optional[AnalysisCache] = None,
) -> ImageMetrics:
    """Full per-image analysis; returns ImageMetrics (never raises)."""
    if cache is not None:
        entry = cache.lookup(image_path)
        if entry is not None:
            return ImageMetrics(
                path=image_path,
                phash=entry.get("phash"),
                sharpness=entry.get("sharpness"),
                tilt_score=entry.get("tilt_score"),
                dominant_axis=entry.get("dominant_axis"),
                line_count=entry.get("line_count") or 0,
            )

    metrics = ImageMetrics(path=image_path)
    metrics.phash = get_phash_hex(image_path)
    metrics.sharpness = get_sharpness_score(image_path)
    tilt = get_tilt_angle(image_path)
    if tilt is not None:
        metrics.tilt_score, metrics.dominant_axis, metrics.line_count = tilt
    if cache is not None:
        cache.update(image_path, metrics)
    return metrics


def _cluster_by_hash(
    metrics_list: List[ImageMetrics],
    threshold: int = HASH_HAMMING_THRESHOLD,
) -> List[List[ImageMetrics]]:
    """Union-find clustering on Hamming distance ≤ threshold."""
    n = len(metrics_list)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    hashes = [m.phash for m in metrics_list]
    for i in range(n):
        for j in range(i + 1, n):
            d = _hamming(hashes[i], hashes[j])
            if d is not None and d <= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    buckets: Dict[int, List[ImageMetrics]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(metrics_list[i])
    return list(buckets.values())


def get_similarity_clusters(
    folder_path: str,
    config: Optional[dict] = None,
    cache: Optional[AnalysisCache] = None,
) -> List[SimilarityCluster]:
    """Analyze all images in ``folder_path`` and return similarity clusters."""
    folder = Path(folder_path)
    if not folder.is_dir():
        logger.warning("Folder does not exist: %s", folder_path)
        return []

    files = sorted(
        str(p) for p in folder.iterdir()
        if p.is_file() and _is_analyzable_file(str(p))
    )
    if not files:
        return []

    logger.info("Analyzing %d images in %s", len(files), folder_path)
    metrics_list = [analyze_image(f, cache=cache) for f in files]
    threshold = (
        int(config.get("clustering", {}).get("hamming_threshold", HASH_HAMMING_THRESHOLD))
        if config else HASH_HAMMING_THRESHOLD
    )
    groups = _cluster_by_hash(metrics_list, threshold=threshold)
    clusters = [
        SimilarityCluster(folder=folder_path, members=g)
        for g in groups if len(g) >= 2
    ]
    clusters.sort(key=lambda c: len(c.members), reverse=True)
    logger.info("Found %d clusters in %s", len(clusters), folder_path)
    return clusters


def find_quality_issues(
    folder_path: str,
    cache: Optional[AnalysisCache] = None,
    config: Optional[dict] = None,
) -> List[QualityReviewItem]:
    """Find blurry/shaky photos in ``folder_path`` with optional sharper alternatives.

    A photo is flagged when its Laplacian-variance sharpness falls below
    ``LOW_SHARPNESS_THRESHOLD`` (configurable via ``[quality] sharpness_threshold``
    in ``snapsort.toml``).  For each flagged photo, any same-folder photos that are
    above the threshold *and* have a similar pHash are listed as alternatives.
    """
    folder = Path(folder_path)
    if not folder.is_dir():
        return []
    files = sorted(
        str(p) for p in folder.iterdir()
        if p.is_file() and _is_analyzable_file(str(p))
    )
    if not files:
        return []

    threshold = float(
        (config or {}).get("quality", {}).get("sharpness_threshold", LOW_SHARPNESS_THRESHOLD)
    )
    metrics_list = [analyze_image(f, cache=cache) for f in files]
    bad_set = {
        m for m in metrics_list
        if m.sharpness is not None and m.sharpness < threshold
    }
    good = [m for m in metrics_list if m not in bad_set]

    issues: List[QualityReviewItem] = []
    for bm in sorted(bad_set, key=lambda m: m.sharpness or 0.0):
        alts: List[ImageMetrics] = []
        for gm in good:
            if bm.phash and gm.phash:
                d = _hamming(bm.phash, gm.phash)
                # Wider threshold (3×) to catch same-scene shots with composition shift
                if d is not None and d <= HASH_HAMMING_THRESHOLD * 3:
                    alts.append(gm)
        alts.sort(key=lambda m: -(m.sharpness or 0.0))
        issues.append(QualityReviewItem(
            folder=folder_path,
            bad=bm,
            alternatives=alts[:IMAGES_PER_CLUSTER_MAX],
        ))
    logger.debug("Quality issues in %s: %d", folder_path, len(issues))
    return issues


def analyze_directory_full(
    root_path: str,
    progress_cb=None,
    use_cache: bool = True,
    config: Optional[dict] = None,
) -> "tuple[List[QualityReviewItem], List[SimilarityCluster]]":
    """Recursively analyze image folders for both quality issues and similarity clusters.

    Runs both passes per folder in a single scan so the cache is shared
    and images are analyzed only once.

    ``progress_cb(done, total, new_issues, new_clusters)`` is called after
    each folder so the UI can stream results immediately.

    Returns ``(quality_issues, clusters)`` tuple.
    """
    root_path = str(Path(root_path).expanduser().absolute())
    cache: Optional[AnalysisCache] = AnalysisCache(root_path) if use_cache else None
    folders = _iter_folders_with_images(root_path)
    total = len(folders)
    all_quality: List[QualityReviewItem] = []
    all_clusters: List[SimilarityCluster] = []
    if total:
        logger.info("Full analysis: %d folders under %s", total, root_path)
    for i, dirpath in enumerate(folders, start=1):
        issues = find_quality_issues(dirpath, cache=cache, config=config)
        all_quality.extend(issues)
        clusters = get_similarity_clusters(dirpath, cache=cache, config=config)
        all_clusters.extend(clusters)
        if progress_cb is not None:
            try:
                progress_cb(i, total, issues, clusters)
            except Exception:
                logger.debug("progress_cb error", exc_info=True)
    if cache is not None:
        if total:
            logger.info("Cache: %d hits, %d misses", cache.hits, cache.misses)
        cache.save()
    return all_quality, all_clusters


def _iter_folders_with_images(root_path: str) -> List[str]:
    """Return all folders under ``root_path`` that contain analyzable images."""
    root = Path(root_path)
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
    return folders


def analyze_directory(
    root_path: str,
    progress_cb=None,
    use_cache: bool = True,
    config: Optional[dict] = None,
) -> Dict[str, List[SimilarityCluster]]:
    """Recursively analyze all image folders under ``root_path``.

    ``progress_cb(done, total)`` is called after each folder (for UI
    progress reporting). Results are cached per-file by mtime+size.
    """
    root_path = str(Path(root_path).expanduser().absolute())
    cache: Optional[AnalysisCache] = AnalysisCache(root_path) if use_cache else None
    result: Dict[str, List[SimilarityCluster]] = {}
    folders = _iter_folders_with_images(root_path)
    total = len(folders)
    if total:
        logger.info("Analyzing %d folders under %s", total, root_path)
    for i, dirpath in enumerate(folders, start=1):
        clusters = get_similarity_clusters(dirpath, cache=cache, config=config)
        if clusters:
            result[dirpath] = clusters
        if progress_cb is not None:
            try:
                progress_cb(i, total)
            except Exception:
                logger.debug("progress_cb failed", exc_info=True)
    if cache is not None:
        if total:
            logger.info("Cache: %d hits, %d misses", cache.hits, cache.misses)
        cache.save()
    return result
