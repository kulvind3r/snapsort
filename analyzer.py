"""
analyzer.py — Perceptual hashing, similarity clustering, and directory analysis.

Delegates image quality metrics (sharpness, tilt, data models) to ``quality``.
This module owns pHash computation, union-find clustering, and the top-level
``analyze_directory`` entry point consumed by ``ui``.
"""

from __future__ import annotations

import logging
import math
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

# Colour-histogram clustering constants
HISTOGRAM_BINS = 16          # bins per channel  (16×3 = 48 floats total)
HISTOGRAM_SIMILARITY_THRESHOLD = 0.93
# Bhattacharyya coefficient ≥ this → same-scene cluster.
# Real burst shots typically score 0.98+; different outdoor scenes score 0.65-0.82.
# Configurable via snapsort.toml [clustering] histogram_threshold.

MAX_HISTOGRAM_CLUSTER_SIZE = 9
# Clusters are capped at this size: the best-quality photos are kept for
# review; excess members remain in the folder and will re-cluster on the
# next culling run.  Keeping this equal to PAGE_SIZE means one clean
# grid page per cluster with no tournament rounds needed for normal bursts.

COMBINED_HASH_THRESHOLD = 20
# Hamming distance upper-bound used for the pHash confirmation step inside
# get_histogram_clusters.  A candidate pair must pass BOTH:
#   histogram Bhattacharyya ≥ HISTOGRAM_SIMILARITY_THRESHOLD  (same colour)
#   pHash Hamming ≤ COMBINED_HASH_THRESHOLD                   (same structure)
# The old HASH_HAMMING_THRESHOLD (10) is intentionally stricter and is kept
# for the standalone pHash path used by find_quality_issues.
# Configurable via snapsort.toml [clustering] combined_hash_threshold.


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
    bad = [
        m for m in metrics_list
        if m.sharpness is not None and m.sharpness < threshold
    ]
    bad_paths = {m.path for m in bad}          # str keys — always hashable
    good = [m for m in metrics_list if m.path not in bad_paths]

    issues: List[QualityReviewItem] = []
    for bm in sorted(bad, key=lambda m: m.sharpness or 0.0):
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


def get_color_histogram(
    image_path: str,
    cache: Optional[AnalysisCache] = None,
) -> Optional[List[float]]:
    """Return a normalised RGB histogram (``HISTOGRAM_BINS`` bins per channel).

    Result is stored in *cache* (keyed by mtime+size) so repeated runs skip
    the PIL open+resize entirely.  Uses PIL — no OpenCV dependency.
    Returns None on failure.
    """
    expected_len = HISTOGRAM_BINS * 3
    if cache is not None:
        cached = cache.get_histogram(image_path)
        # Reject cached entries whose length doesn't match current HISTOGRAM_BINS
        # (guards against stale entries from a different bin-count setting).
        if cached is not None and len(cached) == expected_len:
            return cached

    if Image is None:
        return None
    try:
        src = Image.open(image_path)
        src.load()
        src = src.resize((96, 96), Image.LANCZOS)
        if src.mode != "RGB":
            src = src.convert("RGB")
        raw = src.histogram()  # 256 * 3 values: R then G then B
        factor = 256 // HISTOGRAM_BINS
        combined: List[float] = []
        for ch in range(3):
            offset = ch * 256
            combined += [
                sum(raw[offset + i * factor: offset + (i + 1) * factor])
                for i in range(HISTOGRAM_BINS)
            ]
        total = sum(combined) or 1
        result = [v / total for v in combined]
        if cache is not None:
            cache.set_histogram(image_path, result)
        return result
    except Exception:
        logger.debug("Histogram failed for %s", image_path, exc_info=True)
        return None


def histogram_similarity(h1: List[float], h2: List[float]) -> float:
    """Bhattacharyya coefficient ∈ [0, 1].  1 = identical distribution."""
    return sum(math.sqrt(a * b) for a, b in zip(h1, h2))


def _cluster_by_histogram(
    file_paths: List[str],
    threshold: float = HISTOGRAM_SIMILARITY_THRESHOLD,
    cache: Optional[AnalysisCache] = None,
    progress_cb=None,
) -> List[List[str]]:
    """Union-find clustering on colour-histogram Bhattacharyya similarity.

    ``progress_cb(images_done, images_total)`` is called after each histogram
    is computed so the UI can show per-image progress during the slow I/O phase.
    """
    n = len(file_paths)
    histograms: List[Optional[List[float]]] = []
    for i, p in enumerate(file_paths):
        histograms.append(get_color_histogram(p, cache=cache))
        if progress_cb is not None:
            try:
                progress_cb(i + 1, n)
            except Exception:
                pass
    logger.debug(
        "Histograms ready: %d files, %d comparisons to run", n, n * (n - 1) // 2
    )
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        if histograms[i] is None:
            continue
        for j in range(i + 1, n):
            if histograms[j] is None:
                continue
            if histogram_similarity(histograms[i], histograms[j]) >= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    buckets: Dict[int, List[str]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(file_paths[i])
    return list(buckets.values())


def get_histogram_clusters(
    folder_path: str,
    config: Optional[dict] = None,
    cache: Optional[AnalysisCache] = None,
    progress_cb=None,
) -> List[SimilarityCluster]:
    """Cluster images in *folder_path* by colour-histogram similarity confirmed by pHash.

    Two-pass approach:
      Pass 1 — colour histogram (Bhattacharyya ≥ HISTOGRAM_SIMILARITY_THRESHOLD):
               fast per-image scan; ``progress_cb(done, total)`` fires here.
      Pass 2 — pHash Hamming distance (≤ COMBINED_HASH_THRESHOLD):
               re-clusters each histogram candidate group by structural similarity
               to reject "same colour palette, different subject" false positives.

    Only images that end up in a confirmed multi-photo group have full
    ``ImageMetrics`` computed (sharpness/tilt) so the expensive CV pass
    is skipped for photos with no near-duplicates.
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

    logger.info("Histogram scan: %d images in %s", len(files), folder_path)
    threshold = float(
        (config or {}).get("clustering", {}).get(
            "histogram_threshold", HISTOGRAM_SIMILARITY_THRESHOLD
        )
    )
    groups = _cluster_by_histogram(
        files, threshold=threshold, cache=cache, progress_cb=progress_cb
    )
    multi = [g for g in groups if len(g) >= 2]
    if not multi:
        return []

    # Compute full ImageMetrics (pHash, sharpness, tilt) only for photos
    # that are in a histogram candidate group.
    needed = {p for g in multi for p in g}
    metrics_map: Dict[str, ImageMetrics] = {
        p: analyze_image(p, cache=cache) for p in needed
    }

    # Pass 2 — pHash confirmation.
    # Re-cluster each histogram group using structural (pHash) similarity so
    # that photos with the same colour distribution but different subjects are
    # separated into distinct clusters (or dropped as singletons).
    hash_threshold = int(
        (config or {}).get("clustering", {}).get(
            "combined_hash_threshold", COMBINED_HASH_THRESHOLD
        )
    )
    verified: List[SimilarityCluster] = []
    for group_paths in multi:
        group_metrics = [metrics_map[p] for p in group_paths]
        subgroups = _cluster_by_hash(group_metrics, threshold=hash_threshold)
        for sg in subgroups:
            if len(sg) >= 2:
                verified.append(SimilarityCluster(folder=folder_path, members=sg))

    if not verified:
        logger.info(
            "Combined clusters in %s: %d histogram group(s), 0 survived pHash check",
            folder_path, len(multi),
        )
        return []

    logger.info(
        "Combined clusters in %s: %d histogram candidate(s) → %d after pHash confirmation",
        folder_path, len(multi), len(verified),
    )

    # Truncate oversized clusters: keep the best-quality members up to
    # MAX_HISTOGRAM_CLUSTER_SIZE; the rest stay on disk and will re-cluster
    # on the next culling run once the kept photos are discarded.
    result = []
    for cl in verified:
        if len(cl.members) > MAX_HISTOGRAM_CLUSTER_SIZE:
            excess = len(cl.members) - MAX_HISTOGRAM_CLUSTER_SIZE
            best = cl.sorted_members()[:MAX_HISTOGRAM_CLUSTER_SIZE]
            logger.info(
                "Cluster of %d truncated to %d best-quality photos "
                "(%d left for next run): %s",
                len(cl.members), MAX_HISTOGRAM_CLUSTER_SIZE, excess, folder_path,
            )
            result.append(SimilarityCluster(folder=folder_path, members=best))
        else:
            result.append(cl)

    result.sort(key=lambda c: len(c.members), reverse=True)
    return result


def analyze_directory_full(
    root_path: str,
    progress_cb=None,
    scan_progress_cb=None,
    use_cache: bool = True,
    config: Optional[dict] = None,
) -> "tuple[List[SimilarityCluster], List[QualityReviewItem]]":
    """Recursively analyse image folders for near-duplicate clusters and quality issues.

    Step order returned matches review order:
      1. ``SimilarityCluster`` list — colour-histogram based near-duplicate groups.
      2. ``QualityReviewItem`` list — blurry / shaky photos with alternatives.

    ``progress_cb(done, total, new_clusters, new_issues)`` is called after each
    folder so the UI can stream results as they arrive.

    ``scan_progress_cb(images_done, images_total)`` is called after each
    *individual image* histogram is computed — useful for per-image progress
    bars when a folder has many photos (e.g. 1,000+).
    """
    root_path = str(Path(root_path).expanduser().absolute())
    cache: Optional[AnalysisCache] = AnalysisCache(root_path) if use_cache else None
    folders = _iter_folders_with_images(root_path)
    total = len(folders)
    all_clusters: List[SimilarityCluster] = []
    all_quality: List[QualityReviewItem] = []
    if total:
        logger.info("Full analysis: %d folders under %s", total, root_path)

    # Count total analysable images upfront so scan_progress_cb can report
    # absolute progress across all folders, not just within the current one.
    folder_sizes: List[int] = []
    if scan_progress_cb is not None:
        for d in folders:
            try:
                cnt = sum(
                    1 for e in Path(d).iterdir()
                    if e.is_file() and _is_analyzable_file(str(e))
                )
            except OSError:
                cnt = 0
            folder_sizes.append(cnt)
    total_images = sum(folder_sizes) if folder_sizes else 0
    images_scanned = [0]   # mutable so the closure can update it

    for i, dirpath in enumerate(folders, start=1):
        # Build a per-image callback that offsets into the global image count
        if scan_progress_cb is not None:
            folder_offset = sum(folder_sizes[:i - 1])

            def _img_cb(folder_done: int, _folder_total: int,
                        _offset: int = folder_offset) -> None:
                scan_progress_cb(_offset + folder_done, total_images)
        else:
            _img_cb = None

        clusters = get_histogram_clusters(
            dirpath, config=config, cache=cache, progress_cb=_img_cb
        )
        all_clusters.extend(clusters)
        issues = find_quality_issues(dirpath, cache=cache, config=config)
        all_quality.extend(issues)
        if progress_cb is not None:
            try:
                progress_cb(i, total, clusters, issues)
            except Exception:
                logger.debug("progress_cb error", exc_info=True)
    if cache is not None:
        if total:
            logger.info("Cache: %d hits, %d misses", cache.hits, cache.misses)
        cache.save()
    return all_clusters, all_quality


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
