from __future__ import annotations
import sys
from pathlib import Path

# Ensure project root is on sys.path so top-level modules are importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import piexif
from PIL import Image, ImageFilter


def make_jpeg(
    path: Path,
    *,
    color: tuple = (100, 100, 100),
    date_str: str | None = None,
    gps: tuple[float, float] | None = None,
    blur: bool = False,
) -> Path:
    from PIL import ImageDraw
    img = Image.new("RGB", (320, 240), color)
    draw = ImageDraw.Draw(img)
    # Draw a few short diagonal segments (not axis-aligned) so that
    # sharpness is non-zero and differs between blurred/non-blurred,
    # while keeping pHash dominated by the base color.
    dark = tuple(max(0, c - 80) for c in color)
    for i in range(5):
        x0, y0 = 30 + i * 60, 30 + i * 30
        draw.line([(x0, y0), (x0 + 15, y0 + 15)], fill=dark, width=2)
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