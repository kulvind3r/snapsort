# SnapSort — Engineering Brief (Active)

> **For the LLM:** This document is the single source of truth for what needs to be done.
> Read only the files directly relevant to your current task. Do not re-read this document
> mid-task. Complete one task, verify the file is saved, then move to the next.

---

## 1. What the App Does

Two-stage offline photo organiser for consumer smartphone formats (`.jpg .jpeg .png .webp .heic .heif`):

- **Stage 1 (headless):** Discovers photos, extracts date/GPS from EXIF → filename → mtime, clusters by date and Haversine GPS proximity into `YYYY-MM-DD/` and `Trip_YYYY-MM-DD_to_YYYY-MM-DD/` folders, moves files.
- **Stage 2 (Tkinter GUI):** Computes pHash similarity clusters per folder, scores sharpness (Laplacian variance) and tilt (Hough lines), presents up to 3 images side-by-side for keyboard-driven keep/reject. Rejected photos go to `_Discarded/`, never deleted.

---

## 2. Current Module Map

| File | Lines | Responsibility |
|---|---|---|
| `main.py` | 209 | CLI entry point, two-stage orchestration |
| `organizer.py` | 602 | File discovery, EXIF/GPS extraction, date clustering, file moves |
| `analyzer.py` | 536 | pHash, sharpness, tilt, similarity clustering |
| `ui.py` | 542 | Tkinter culling UI, thumbnail rendering, hotkeys |
| `paths.py` | 96 | Path constants, `_Discarded/` naming, containment guard |
| `journal.py` | 175 | Append-only move journal, `safe_move()`, persisted undo stack |
| `config.py` | 175 | `snapsort.toml` loader, typed accessors, built-in defaults |
| `analysis_cache.py` | 151 | Per-file analysis cache keyed on `(path, mtime, size)` |
| `logging_setup.py` | 120 | `RotatingFileHandler`, startup banner, `sys.excepthook` |

**Supporting files:** `pyproject.toml`, `snapsort.spec`, `requirements.txt`, `tests/smoke_test.py`

---

## 3. Data Safety Contract (enforced — do not break)

1. **No file is ever deleted.** `os.remove`, `os.unlink`, `shutil.rmtree` must never appear in production modules.
2. **All moves go through `journal.safe_move()`** — it journals before moving, validates path containment, and logs.
3. **Rejected photos land in `<output_root>/_Discarded/<event_folder>/<filename>`.**
4. **`_Discarded/` and `_Rejects/` are pruned from `os.walk`** in both `organizer.py` and `analyzer.py` so re-runs never reprocess discarded photos.
5. **Undo stack is persisted** to `.snapsort_undo.json` via `journal.push_undo()` / `journal.pop_undo()`.

---

## 4. Pending Tasks

Complete tasks in order. Verify the file exists on disk before marking done.

---

### Task 1 — GitHub Actions Windows Build Workflow

Create `.github/workflows/build-windows.yml` with exactly this content:

```yaml
name: Build Windows Binary

on:
  push:
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build-windows:
    runs-on: windows-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: "pip"

      - name: Install dependencies
        run: |
          pip install uv
          uv pip install --system -e ".[dev]"

      - name: Build with PyInstaller
        run: pyinstaller snapsort.spec --clean --noconfirm

      - name: Verify binary exists
        run: |
          if (-Not (Test-Path "dist\SnapSort\SnapSort.exe")) {
            Write-Error "Build failed: SnapSort.exe not found"
            exit 1
          }
        shell: pwsh

      - name: Upload build artifact
        uses: actions/upload-artifact@v4
        with:
          name: SnapSort-windows-${{ github.ref_name }}
          path: dist\SnapSort\
          retention-days: 30

      - name: Create GitHub Release (on version tag only)
        if: startsWith(github.ref, 'refs/tags/v')
        uses: softprops/action-gh-release@v2
        with:
          files: dist\SnapSort\*
          generate_release_notes: true
```

**Note:** PyInstaller cannot cross-compile — this workflow MUST run on `windows-latest`. The binary is built in CI, not locally on macOS.

---

### Task 2 — Replace Smoke Test with Pytest Suite

**Delete `tests/smoke_test.py` entirely.** Do not attempt to fix it. Its hand-built EXIF byte layout is incompatible with Pillow 11 GPS IFD parsing and the approach is fundamentally wrong.

Add `piexif>=1.1.3` to the `[dev]` / `[project.optional-dependencies]` dev group in `pyproject.toml` if not already present.

Create the following files. Write each file completely before moving to the next.

#### `tests/conftest.py`

```python
from __future__ import annotations
import pytest
import piexif
from pathlib import Path
from PIL import Image, ImageFilter


def make_jpeg(
    path: Path,
    *,
    color: tuple = (100, 100, 100),
    date_str: str | None = None,
    gps: tuple[float, float] | None = None,
    blur: bool = False,
) -> Path:
    img = Image.new("RGB", (320, 240), color)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(6))

    exif_bytes = b""
    if date_str or gps:
        exif_dict: dict = {"0th": {}, "Exif": {}, "GPS": {}}
        if date_str:
            raw = date_str.encode("ascii")
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = raw
            exif_dict["0th"][piexif.ImageIFD.DateTime] = raw
        if gps:
            lat, lon = gps

            def to_rat(v: float) -> tuple:
                d = int(abs(v))
                m = int((abs(v) - d) * 60)
                s = round(((abs(v) - d) * 60 - m) * 60 * 100)
                return ((d, 1), (m, 1), (s, 100))

            exif_dict["GPS"] = {
                piexif.GPSIFD.GPSLatitudeRef:  b"N" if lat >= 0 else b"S",
                piexif.GPSIFD.GPSLatitude:     to_rat(lat),
                piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
                piexif.GPSIFD.GPSLongitude:    to_rat(lon),
            }
        exif_bytes = piexif.dump(exif_dict)

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "JPEG", exif=exif_bytes)
    return path


@pytest.fixture
def make_jpeg_file(tmp_path):
    """Fixture: returns make_jpeg bound to tmp_path."""
    def _make(name: str, **kwargs) -> Path:
        return make_jpeg(tmp_path / name, **kwargs)
    return _make
```

#### `tests/unit/test_data_safety.py`

```python
"""Static + runtime data-safety contract verification."""
from __future__ import annotations
import ast
import pathlib
import pytest

SOURCE_FILES = ["organizer.py", "analyzer.py", "ui.py", "main.py", "journal.py"]
FORBIDDEN_CALLS = {"os.remove", "os.unlink", "shutil.rmtree"}


@pytest.mark.parametrize("source_file", SOURCE_FILES)
def test_no_destructive_calls(source_file):
    src = pathlib.Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                call = f"{node.value.id}.{node.attr}"
                assert call not in FORBIDDEN_CALLS, (
                    f"{source_file} line {node.lineno}: forbidden call '{call}'"
                )


def test_discarded_not_reprocessed(tmp_path, make_jpeg_file):
    from organizer import organize_directory

    make_jpeg_file("photo.jpg", date_str="2024:06:01 10:00:00")
    organize_directory(str(tmp_path))

    discarded = tmp_path / "_Discarded" / "2024-06-01" / "photo.jpg"
    discarded.parent.mkdir(parents=True, exist_ok=True)
    discarded.write_bytes(b"\xff\xd8" + b"\x00" * 50)

    organize_directory(str(tmp_path))

    assert discarded.exists(), "_Discarded file was moved on re-run"
```

#### `tests/unit/test_clustering.py`

```python
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
```

#### `tests/integration/test_organize.py`

```python
from organizer import organize_directory


def test_photos_land_in_dated_subfolder(tmp_path, make_jpeg_file):
    make_jpeg_file("a.jpg", date_str="2024:06:01 10:00:00")
    make_jpeg_file("b.jpg", date_str="2024:06:01 11:00:00")
    organize_directory(str(tmp_path))
    event = tmp_path / "2024-06-01"
    assert event.is_dir()
    assert len(list(event.glob("*.jpg"))) == 2


def test_trip_folder_created_for_nearby_gps(tmp_path, make_jpeg_file):
    make_jpeg_file("d1.jpg", date_str="2024:03:10 09:00:00", gps=(35.6, 139.7))
    make_jpeg_file("d2.jpg", date_str="2024:03:11 10:00:00", gps=(35.7, 139.8))
    organize_directory(str(tmp_path))
    assert len(list(tmp_path.glob("Trip_*"))) == 1


def test_corrupt_file_does_not_crash(tmp_path):
    (tmp_path / "bad.jpg").write_bytes(b"\xff\xd8\xff\xe0NOTREALJPEG")
    organize_directory(str(tmp_path))  # must not raise


def test_unsupported_extension_ignored(tmp_path):
    (tmp_path / "notes.txt").write_text("ignore me")
    organize_directory(str(tmp_path))
    assert (tmp_path / "notes.txt").exists()
```

#### `tests/integration/test_analyzer.py`

```python
from analyzer import get_similarity_clusters
from pathlib import Path


def test_near_duplicates_form_cluster(tmp_path, make_jpeg_file):
    make_jpeg_file("a.jpg", color=(200, 30, 30))
    make_jpeg_file("b.jpg", color=(202, 30, 30))   # near-identical
    make_jpeg_file("c.jpg", color=(10, 200, 10))   # clearly different
    clusters = get_similarity_clusters(str(tmp_path))
    assert len(clusters) >= 1
    names = {Path(m.path).name for cl in clusters for m in cl.members}
    assert "a.jpg" in names and "b.jpg" in names


def test_blurry_image_ranked_last(tmp_path, make_jpeg_file):
    make_jpeg_file("sharp.jpg", color=(200, 30, 30), blur=False)
    make_jpeg_file("blurry.jpg", color=(200, 30, 30), blur=True)
    clusters = get_similarity_clusters(str(tmp_path))
    assert clusters, "Expected a similarity cluster"
    best = clusters[0].best_pick()
    assert best is not None
    assert "sharp" in best.path
```

Run the full suite with: `pytest -q`

---

### Task 3 — Refactor Oversized Files (250-line limit, single responsibility)

`organizer.py` (602 lines) and `analyzer.py` (536 lines) and `ui.py` (542 lines) each contain multiple distinct responsibilities and must be split. The 250-line-per-file limit exists to keep each file readable and manageable by a local LLM in a single context window.

**Do not change any logic or function signatures. This is a structural move only.**

#### 3a — Split `organizer.py` → 3 files

| New file | What goes in it | Approx lines |
|---|---|---|
| `metadata.py` | `PhotoRecord` dataclass, `discover_photos()`, `_populate_metadata()`, all EXIF/GPS helpers (`_extract_exif_datetime`, `_parse_filename_date`, `_safe_mtime_date`, `_dms_to_decimal`, `_extract_gps`, `get_image_date`, `get_image_gps`) | ~220 |
| `clustering.py` | `haversine()`, `centroid()`, `_group_by_date()`, `_day_centroid()`, `_mergeable()`, `cluster_records()`, `_folder_name()` | ~160 |
| `organizer.py` (trimmed) | `discover_photos` re-export, `_resolve_unique_destination()`, `organize_directory()`, `summarize_clusters()`. Imports from `metadata` and `clustering`. | ~120 |

#### 3b — Split `analyzer.py` → 2 files

| New file | What goes in it | Approx lines |
|---|---|---|
| `quality.py` | `ImageMetrics`, `SimilarityCluster`, all OpenCV helpers (`_load_cv`, `_compute_gray`, `_dispose`), `get_sharpness_score()`, `get_tilt_angle()`, `_segment_angle_deg()`, `_deviation_from_axis()` | ~220 |
| `analyzer.py` (trimmed) | pHash helpers (`get_phash_hex`, `_hamming`, `_popcount`), `analyze_image()`, `_cluster_by_hash()`, `get_similarity_clusters()`, `analyze_directory()`. Imports `ImageMetrics`, `SimilarityCluster` from `quality`. | ~200 |

#### 3c — Split `ui.py` → 2 files

| New file | What goes in it | Approx lines |
|---|---|---|
| `widgets.py` | `make_thumbnail()`, all layout constants (`THUMB_WIDTH`, `BG`, `FG`, etc.), `UndoEntry` dataclass | ~60 |
| `ui.py` (trimmed) | `PhotoCullerUI` class, `run_culler()`, `pick_folder_and_run()`. Imports from `widgets`. | ~240 |

#### Rules for the refactor
- Update all `import` statements across all files that reference the moved symbols.
- `pyproject.toml` `[tool.setuptools] py-modules` list must be updated to include all new module names.
- Run `python -c "from organizer import organize_directory; from analyzer import analyze_directory; from ui import run_culler"` after the refactor to verify imports are not broken.
- Do not rename any public functions or classes.

---

## 5. Manual Testing Checklist (do not automate)

After all tasks are complete, the developer will manually verify:

- [ ] `pytest -q` passes with zero failures
- [ ] `python main.py ~/Pictures --dry-run` runs without error on macOS
- [ ] Windows binary (`SnapSort.exe`) launches, log file created at `%LOCALAPPDATA%\SnapSort\Logs\snapsort.log`
- [ ] HEIC photos from an iPhone are discovered and organized correctly
- [ ] Culling UI does not freeze on a folder of 100+ photos (background thread)
- [ ] Undo after app restart — journal file persists undo history correctly
- [ ] Re-running Stage 1 on an already-organized folder does not touch `_Discarded/`
