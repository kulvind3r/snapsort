# Product Design Document & Specification: Local Auto-Organizing Photo Culler

## 1. Master One-Shot Prompt for Local LLM
> **Instructions for LLM:** You are an expert Python desktop application engineer. Your task is to implement a complete, production-ready, standalone Windows photo organization and culling application by following the modular specifications in this document. 
> 
> Write clean, modular, fully realized Python code without omitting code, using placeholders (`# TODO`), or abbreviating functions. Generate code across four logical files: `organizer.py`, `analyzer.py`, `ui.py`, and `main.py`. Ensure all error handling, EXIF edge cases, and OpenCV memory allocations are cleanly handled.

---

## 2. Architecture & Pipeline Overview

The application processes photo libraries via a strict two-stage sequential workflow:

[Target Directory] ──► Stage 1: Headless Metadata & Folder Structuring
│
▼
[Organized Folders] ──► Stage 2: Assisted UI Visual Culling ──► [Final Library]

* **Stage 1 (Headless Engine):** Scans all files recursively, extracts EXIF/filename dates and GPS coordinates, clusters photos by dates and geographic proximity into event/trip folders, and moves files physically on disk.
* **Stage 2 (Interactive GUI):** Scans organized folders for visually similar photo clusters (pHash), measures sharpness (Laplacian variance) and tilt (Hough lines), and displays cluster comparisons in a keyboard-driven interface for rapid culling.

---

## 3. System Components & Algorithm Specifications

### Module 1: File Discovery & Metadata Extraction (`organizer.py`)
* **File Discovery:** Recursively traverse input root directory for extensions: `.jpg`, `.jpeg`, `.png`, `.webp`, `.arw`, `.cr2`, `.nef`.
* **Timestamp Priority Resolution:**
  1. **EXIF Metadata:** Read `DateTimeOriginal` or `DateTimeDigitized` tags via `Pillow` or `exifread`.
  2. **Filename Regex:** Match pattern `(19|20)\d\d[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])`.
  3. **OS File Metadata:** Fall back to `os.path.getmtime()`.
* **GPS Extraction:** Parse `GPSInfo` EXIF tags into Decimal Degrees:
  $$\text{Decimal Degrees} = \text{Degrees} + \frac{\text{Minutes}}{60} + \frac{\text{Seconds}}{3600}$$
  *(Apply negative sign for South latitude or West longitude).*

### Module 2: Intelligent Date & Trip Clustering Engine (`organizer.py`)
* **Haversine Distance Formula:** Calculate geographic distance $d$ in kilometers between coordinates $(\phi_1, \lambda_1)$ and $(\phi_2, \lambda_2)$:
  $$d = 2 r \arcsin \left( \sqrt{\sin^2\left(\frac{\Delta \phi}{2}\right) + \cos(\phi_1) \cos(\phi_2) \sin^2\left(\frac{\Delta \lambda}{2}\right)} \right)$$
  *(where Earth radius $r = 6371\text{ km}$)*.

* **Clustering Rules:**
  1. Group photos by calendar date (`YYYY-MM-DD`).
  2. Combine consecutive daily groups into a single **Trip Cluster** folder if:
     * Date gap between consecutive photo groups is $\le 1\text{ day}$, **AND**
     * Geographic centroid distance between Day $N$ and Day $N+1$ is $\le 50\text{ km}$ (if GPS exists) OR consecutive photo dates span 2 to 7 days continuously (if GPS missing).
  3. **Folder Creation & File Movement:**
     * Single Event: Directory named `/YYYY-MM-DD/`
     * Trip Cluster: Directory named `/Trip_YYYY-MM-DD_to_YYYY-MM-DD/`

### Module 3: Similarity & Image Quality Analysis Engine (`analyzer.py`)
* **Visual Similarity Clustering:**
  * Calculate 64-bit Perceptual Hash using `imagehash.phash()`.
  * Compute Hamming distance between images within the same folder.
  * Group images with $\text{Hamming Distance} \le 10$ into a **Similarity Cluster**.
* **Sharpness Metric (Laplacian Variance):**
  * Convert image to grayscale and compute variance of Laplacian operator:
    $$\text{Sharpness Score} = \text{Var}(\nabla^2 I)$$
  * Higher scores indicate crisp focus; low scores indicate blur.
* **Horizon Tilt Score (Hough Line Transform):**
  * Apply Canny edge detection (`cv2.Canny`).
  * Extract lines using `cv2.HoughLinesP`.
  * Filter for line segments near horizontal ($0^\circ \pm 15^\circ$) or vertical ($90^\circ \pm 15^\circ$).
  * Compute average angular deviation from pure horizontal/vertical axes. Lower scores indicate straight photos.

### Module 4: Keyboard-Driven Comparison UI (`ui.py` & `main.py`)
* **Layout:** Native Tkinter / CustomTkinter window displaying up to 3 images side-by-side per similarity cluster.
* **Badges & Annotations:** Display calculated **Sharpness Score**, **Tilt Angle**, and auto-flag the **[RECOMMENDED BEST PICK]** (highest sharpness + lowest tilt score).
* **Hotkey Mapping:**
  * Keys `1`, `2`, `3`: Select corresponding image to KEEP. Automatically move unselected photos in the cluster to a local `_Rejects/` subfolder.
  * `Spacebar`: Automatically accept the recommended best pick and reject others.
  * `S` or `Right-Arrow`: Skip current cluster without moving files.
  * `U`: Undo the last file operation.

---

## 4. Step-by-Step Implementation Roadmap for LLM

Follow these steps sequentially to generate the codebase:

### Step 1: Implement `organizer.py`
Create the metadata extractor and directory migration engine:
```python
import os
import re
import shutil
from math import radians, cos, sin, asin, sqrt
from datetime import datetime, timedelta
from PIL import Image, ExifTags

def get_image_date(file_path):
    # 1. Try EXIF
    # 2. Try Filename Regex YYYYMMDD
    # 3. Fallback to mtime
    pass

def get_image_gps(file_path):
    # Extract GPS latitude/longitude as decimal degrees
    pass

def haversine(lat1, lon1, lat2, lon2):
    # Calculate km distance
    pass

def organize_directory(root_dir, output_dir):
    # Crawl, cluster by date/distance, move to event/trip folders
    pass

### Step 2: Implement `analyzer.py`
Create image quality evaluation algorithms:

```Python
import cv2
import numpy as np
import imagehash
from PIL import Image

def get_sharpness_score(image_path):
    # Convert to grayscale, return cv2.Laplacian(img, cv2.CV_64F).var()
    pass

def get_tilt_angle(image_path):
    # Canny edge detection -> HoughLinesP -> return mean angle deviation from 0/90 deg
    pass

def get_similarity_clusters(folder_path):
    # Compute pHash for folder images, group images with Hamming distance <= 10
    pass
```

### Step 3: Implement `ui.py`
Build the side-by-side Tkinter review interface:
``` python
import tkinter as tk
from tkinter import ttk, filedialog
from PIL import Image, ImageTk
import os
import shutil

class PhotoCullerUI(tk.Tk):
    def __init__(self, organized_folder):
        # Initialize UI, bindings for hotkeys 1, 2, 3, Space, S, U
        pass

    def load_cluster(self, cluster):
        # Render up to 3 photos side-by-side with scores and badges
        pass

    def process_selection(self, keep_index):
        # Move non-selected cluster photos to _Rejects folder
        pass
```

### Step 4: Implement `main.py`
Connect stage 1 execution with stage 2 UI launch:
``` python
import sys
import os
from organizer import organize_directory
from ui import PhotoCullerUI

def main():
    # Prompt for source directory
    # Run organize_directory()
    # Launch PhotoCullerUI on organized directory
    pass

if __name__ == "__main__":
    main()
```