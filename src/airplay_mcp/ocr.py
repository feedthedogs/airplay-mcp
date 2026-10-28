"""Optional OCR (``pip install airplay-mcp[ocr]``): text costs far fewer tokens than pixels."""

from __future__ import annotations

import functools
from typing import Any

import numpy as np
from PIL import Image


@functools.cache
def _engine() -> Any | None:
    try:
        from rapidocr import RapidOCR  # rapidocr >= 2
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR  # legacy package name
        except ImportError:
            return None
    return RapidOCR()


def available() -> bool:
    try:
        import importlib.util

        return any(importlib.util.find_spec(m) for m in ("rapidocr", "rapidocr_onnxruntime"))
    except ValueError:
        return False


def read_text(img: Image.Image, min_score: float = 0.5) -> list[tuple[float, float, str]]:
    """Return (x, y, text) per detected line, normalized to the image, in reading order."""
    engine = _engine()
    if engine is None:
        raise RuntimeError("OCR not installed: pip install 'airplay-mcp[ocr]'")
    w, h = img.size
    out = engine(np.asarray(img.convert("RGB")))
    if isinstance(out, tuple):  # legacy: (results | None, elapsed)
        rows = [(box, text, score) for box, text, score in (out[0] or [])]
    else:
        boxes = out.boxes if out.boxes is not None else []
        rows = list(zip(boxes, out.txts or (), out.scores or ()))
    lines = []
    for box, text, score in rows:
        if float(score) < min_score:
            continue
        pts = np.asarray(box, dtype=float)
        lines.append((pts[:, 0].min() / w, pts[:, 1].min() / h, str(text)))
    # Group into rows (~2% of height) and then sort left-to-right.
    lines.sort(key=lambda t: (round(t[1] / 0.02), t[0]))
    return lines
