"""Image shaping for minimal vision-token cost.

Vision models bill by pixel area (Claude: roughly w*h/750 tokens), not by
bytes, so the levers here are cropping and downscaling. JPEG quality only
affects transfer size.
"""

from __future__ import annotations

import io
import math
import string

import numpy as np
from PIL import Image, ImageDraw, ImageFont

Region = tuple[float, float, float, float]  # normalized x, y, w, h


def estimate_tokens(width: int, height: int) -> int:
    return math.ceil(width * height / 750)


def content_box(rgb: np.ndarray, threshold: int = 12) -> tuple[int, int, int, int]:
    """Pixel box (x0, y0, x1, y1) excluding uniform black letterbox bars."""
    lum = rgb.max(axis=2)
    rows = np.flatnonzero(lum.max(axis=1) > threshold)
    cols = np.flatnonzero(lum.max(axis=0) > threshold)
    h, w = lum.shape
    if rows.size == 0 or cols.size == 0:
        return (0, 0, w, h)
    return (int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1)


def crop_region(img: Image.Image, region: Region | None) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Crop a normalized region; returns the crop and its pixel box."""
    w, h = img.size
    if region is None:
        return img, (0, 0, w, h)
    x, y, rw, rh = (min(max(v, 0.0), 1.0) for v in region)
    x0, y0 = int(x * w), int(y * h)
    x1, y1 = max(x0 + 1, min(w, int(math.ceil((x + rw) * w)))), max(y0 + 1, min(h, int(math.ceil((y + rh) * h))))
    return img.crop((x0, y0, x1, y1)), (x0, y0, x1, y1)


def fit(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    scale = max_dim / max(w, h)
    if scale >= 1:
        return img
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)


def draw_grid(img: Image.Image, cols: int, rows: int) -> Image.Image:
    """Overlay a labelled grid: columns A.., rows 1.. (e.g. 9x9 for Sudoku)."""
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    size = max(10, min(w // cols, h // rows) // 4)
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow without FreeType
        font = ImageFont.load_default()
    for c in range(1, cols):
        x = round(c * w / cols)
        draw.line([(x, 0), (x, h)], fill=(255, 0, 255), width=1)
    for r in range(1, rows):
        y = round(r * h / rows)
        draw.line([(0, y), (w, y)], fill=(255, 0, 255), width=1)
    for r in range(rows):
        for c in range(cols):
            label = f"{_col_label(c)}{r + 1}"
            draw.text((c * w / cols + 2, r * h / rows + 1), label, fill=(255, 0, 255), font=font)
    return img


def _col_label(i: int) -> str:
    letters = string.ascii_uppercase
    return letters[i] if i < 26 else letters[i // 26 - 1] + letters[i % 26]


def encode(img: Image.Image, fmt: str = "jpeg", quality: int = 80) -> bytes:
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, format="PNG", optimize=True)
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()
