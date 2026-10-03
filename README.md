# SnapSort

A local, offline photo organiser and duplicate culler for consumer smartphone and camera libraries. No cloud, no accounts, no AI services — runs entirely on your machine.

---

## What It Does

SnapSort processes photos in two stages:

**Stage 1 — Organise (headless)**
Scans a source folder recursively, extracts capture dates from EXIF metadata (falling back to filename patterns and file modification time), clusters photos by date and geographic proximity into named event and trip folders, then moves files on disk.

- Single day → `2024-06-01/`
- Multi-day trip (GPS or consecutive dates) → `Trip_2024-03-10_to_2024-03-12/`
- No date found → `Undated/`

**Stage 2 — Cull (interactive GUI)**
Scans the organised folders for visually similar photos using perceptual hashing (pHash). Displays similar images side-by-side with sharpness and tilt scores, auto-recommending the best pick. You decide which to keep with a single key press.

---

## Supported Formats

`.jpg` `.jpeg` `.png` `.webp` `.heic` `.heif`

HEIC/HEIF support requires `pillow-heif` (installed automatically with the standard setup below).

---

## Installation

```bash
git clone <repo-url>
cd snapsort
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

Or with `uv` (faster):
```bash
uv venv && uv pip install -e .
```

---

## Usage

```bash
# Interactive prompts
python main.py

# Point at a source folder
python main.py ~/Pictures

# Preview moves without touching disk
python main.py ~/Pictures --dry-run

# Separate output location
python main.py ~/Pictures --output ~/Organised

# Stage 1 only (no GUI)
python main.py ~/Pictures --skip-cull

# Stage 2 only (folder already organised)
python main.py --skip-organize
```

### Keyboard shortcuts (Stage 2 GUI)

| Key | Action |
|---|---|
| `1` / `2` / `3` | Keep that photo; move the others to `_Discarded/` |
| `Space` | Accept the auto-recommended best pick |
| `S` or `→` | Skip this cluster (no files moved) |
| `U` | Undo the last move |
| `Q` or `Esc` | Quit (progress is saved; safe to relaunch) |

---

## Data Safety

SnapSort **never deletes photos**. All operations are move-only:

- Rejected duplicates go to `<library>/_Discarded/<event>/<filename>` — never permanently removed.
- Every file move is journaled to `.snapsort_journal.json` **before** it executes.
- The undo stack persists to `.snapsort_undo.json` — undo history survives app restarts.
- Re-running Stage 1 on an already-organised folder skips `_Discarded/` entirely.

---

## Configuration

Create a `snapsort.toml` in your photos folder (or `~/.config/snapsort/config.toml` for a global default) to override defaults:

```toml
[clustering]
hamming_threshold = 12        # 0–256: lower = stricter duplicate detection

[trip]
max_centroid_distance_km = 80.0
max_trip_span_days = 10

[logging]
level = "DEBUG"
```

---

## Logs

Logs are written to a rotating file (5 MB × 3 backups):

| Platform | Location |
|---|---|
| Windows | `%LOCALAPPDATA%\SnapSort\Logs\snapsort.log` |
| macOS | `~/Library/Logs/SnapSort/snapsort.log` |
| Linux | `~/.local/share/snapsort/logs/snapsort.log` |

The log path is shown in the status bar of the culling UI.

---

## Windows Binary

The app ships as a standalone `.exe` built via PyInstaller — no Python installation needed on Windows. Binaries are built automatically on GitHub Actions and published as release artifacts.

**To build locally** (requires a Windows machine or `windows-latest` GitHub Actions runner):

```powershell
pip install uv
uv pip install -e ".[dev]"
pyinstaller snapsort.spec --clean
# Output: dist\SnapSort\SnapSort.exe
```

**To trigger a release build from macOS:**

```bash
git tag v0.2.0
git push origin v0.2.0
# GitHub Actions builds and publishes the artifact automatically
```

---

## Running Tests

```bash
pip install -e ".[dev]"
pytest -q
```

The test suite covers data safety contracts (static AST scan for delete calls, journal-before-move invariant, `_Discarded/` skip on re-run), clustering logic, EXIF date extraction, and similarity detection.

---

## Module Overview

| Module | Responsibility |
|---|---|
| `main.py` | CLI entry point, two-stage orchestration |
| `organizer.py` | Stage 1 orchestration: scan → cluster → move |
| `metadata.py` | File discovery, EXIF/GPS extraction, `PhotoRecord` model |
| `clustering.py` | Haversine distance, trip/event clustering, folder naming |
| `analyzer.py` | pHash hashing, union-find clustering, `analyze_directory` |
| `quality.py` | Sharpness (Laplacian), tilt (Hough lines), `ImageMetrics` model |
| `ui.py` | Tkinter culling UI, background analysis thread |
| `widgets.py` | UI layout constants and thumbnail helper |
| `journal.py` | Append-only move journal, `safe_move()`, persisted undo stack |
| `paths.py` | Path constants, `_Discarded/` naming, containment guard |
| `config.py` | `snapsort.toml` loader, typed accessors, built-in defaults |
| `analysis_cache.py` | Per-file analysis cache keyed on `(path, mtime, size)` |
| `logging_setup.py` | Rotating file handler, startup banner, `sys.excepthook` |
