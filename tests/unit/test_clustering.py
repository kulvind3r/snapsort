"""Clustering pure-logic unit tests — no real files needed."""
from __future__ import annotations
from datetime import date
from organizer import PhotoRecord, cluster_records


def _rec(d: str, lat: float | None = None, lon: float | None = None) -> PhotoRecord:
    rec = PhotoRecord(path="x.jpg", name="x.jpg", ext=".jpg")
    rec.date = date.fromisoformat(d)
    rec.gps = (lat, lon) if lat is not None else None
    return rec


def test_same_day_forms_one_cluster():
    clusters = cluster_records([_rec("2024-03-10"), _rec("2024-03-10")])
    assert len(clusters) == 1


def test_nearby_gps_days_merge():
    clusters = cluster_records([
        _rec("2024-03-10", 35.6, 139.7),
        _rec("2024-03-11", 35.7, 139.8),
    ])
    assert len(clusters) == 1, "Nearby GPS days must merge into a trip"


def test_distant_gps_days_do_not_merge():
    clusters = cluster_records([
        _rec("2024-03-10", 35.6, 139.7),   # Tokyo
        _rec("2024-03-11", 48.8, 2.35),    # Paris
    ])
    assert len(clusters) == 2


def test_gap_over_one_day_splits():
    clusters = cluster_records([_rec("2024-03-10"), _rec("2024-03-12")])
    assert len(clusters) == 2


def test_undated_goes_to_undated_bucket():
    undated = PhotoRecord(path="x.jpg", name="x.jpg", ext=".jpg")
    clusters = cluster_records([_rec("2024-03-10"), undated])
    keys = [str(c[0]) for c in clusters]
    assert "1970-01-01" in keys


def test_filename_date_fallback(tmp_path):
    from organizer import _populate_metadata
    p = tmp_path / "IMG_2024-06-02_holiday.jpg"
    p.write_bytes(b"\xff\xd8" + b"\x00" * 50)
    rec = PhotoRecord(path=str(p), name=p.name, ext=".jpg")
    _populate_metadata(rec)
    assert rec.date is not None
    assert rec.date.isoformat() == "2024-06-02"
    assert rec.source == "filename"