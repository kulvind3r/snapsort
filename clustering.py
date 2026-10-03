"""
clustering.py — Date-based trip/event clustering for SnapSort.

Responsibilities:
  * Haversine distance and centroid helpers.
  * Group PhotoRecords by calendar date.
  * Merge consecutive days into Trip Clusters using date-gap heuristics
    and geographic centroid distance when GPS is available.
  * Folder name generation for event and trip clusters.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Dict, Iterable, List, Optional, Tuple

import config
from metadata import PhotoRecord

logger = logging.getLogger("snapsort.clustering")

MAX_DATE_GAP_DAYS = 1
MAX_TRIP_SPAN_DAYS = 7
MIN_TRIP_SPAN_DAYS = 2
MAX_CENTROID_DISTANCE_KM = 50.0
EARTH_RADIUS_KM = 6371.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two lat/lon coordinates."""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(a)))


def centroid(points: Iterable[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """Return the geographic mean of a list of (lat, lon) points."""
    pts = [p for p in points if p is not None]
    if not pts:
        return None
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def _group_by_date(
    records: List[PhotoRecord],
) -> Dict[date, List[PhotoRecord]]:
    """Group records by capture date; undated records key to 1970-01-01."""
    groups: Dict[date, List[PhotoRecord]] = {}
    for rec in records:
        key = rec.date if rec.date is not None else date(1970, 1, 1)
        groups.setdefault(key, []).append(rec)
    return groups


def _day_centroid(
    day_records: List[PhotoRecord],
) -> Optional[Tuple[float, float]]:
    return centroid([r.gps for r in day_records if r.gps is not None])


def _mergeable(
    day_a: date,
    day_b: date,
    recs_a: List[PhotoRecord],
    recs_b: List[PhotoRecord],
    trip_cfg: Optional[dict] = None,
) -> bool:
    """Return True if two consecutive calendar-day groups should merge."""
    cfg = trip_cfg or {}
    max_gap = config.get(cfg, "trip", "max_date_gap_days")
    max_span = config.get(cfg, "trip", "max_trip_span_days")
    min_span = config.get(cfg, "trip", "min_trip_span_days")
    max_dist = config.get(cfg, "trip", "max_centroid_distance_km")

    gap_days = (day_b - day_a).days
    if gap_days > max_gap:
        return False

    cent_a = _day_centroid(recs_a)
    cent_b = _day_centroid(recs_b)
    if cent_a is not None and cent_b is not None:
        return haversine(cent_a[0], cent_a[1], cent_b[0], cent_b[1]) <= max_dist

    span = gap_days + 1
    return min_span <= span <= max_span


def cluster_records(
    records: List[PhotoRecord],
    config_obj: Optional[dict] = None,
) -> List[Tuple[date, date, List[PhotoRecord]]]:
    """Cluster day-groups into (start, end, records) trip/event tuples.

    Undated photos appear last under a 1970-01-01 pseudo-date bucket.
    """
    groups = _group_by_date(records)
    if not groups:
        return []

    undated = groups.pop(date(1970, 1, 1), None)
    days = sorted(groups.keys())
    clusters: List[Tuple[date, date, List[PhotoRecord]]] = []

    for day in days:
        recs = groups[day]
        if not clusters:
            clusters.append((day, day, list(recs)))
            continue
        prev_start, prev_end, prev_recs = clusters[-1]
        if _mergeable(prev_end, day, prev_recs, recs, config_obj):
            clusters[-1] = (prev_start, day, prev_recs + recs)
        else:
            clusters.append((day, day, list(recs)))

    max_span = config.get(config_obj or {}, "trip", "max_trip_span_days")
    final: List[Tuple[date, date, List[PhotoRecord]]] = []
    for start, end, recs in clusters:
        has_gps = any(r.gps is not None for r in recs)
        span_days = (end - start).days + 1
        if has_gps or span_days <= max_span:
            final.append((start, end, recs))
        else:
            by_day = _group_by_date(recs)
            d = start
            while d <= end:
                chunk_end = min(d + timedelta(days=max_span - 1), end)
                chunk_recs: List[PhotoRecord] = []
                dd = d
                while dd <= chunk_end:
                    chunk_recs.extend(by_day.get(dd, []))
                    dd += timedelta(days=1)
                final.append((d, chunk_end, chunk_recs))
                d = chunk_end + timedelta(days=1)

    if undated:
        final.append((date(1970, 1, 1), date(1970, 1, 1), undated))

    final.sort(key=lambda c: c[0])
    return final


def _folder_name(start: date, end: date) -> str:
    """Human-readable folder name for a date range."""
    if start == date(1970, 1, 1):
        return "Undated"
    if start == end:
        return start.isoformat()
    return "Trip_{}_to_{}".format(start.isoformat(), end.isoformat())
