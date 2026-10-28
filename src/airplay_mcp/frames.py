"""Latest-frame store with cheap, token-oriented change detection.

Every submitted frame is reduced to a small grayscale "signature" (block means
over a coarse grid). Signatures are compared to decide:

* whether the screen has *meaningfully* changed (a new ``seq`` is issued), and
* whether the screen is still moving (used to wait for a settled frame).

Noisy regions (the iOS status bar clock, battery, etc.) are masked out of the
comparison so they never cause a new ``seq`` on their own.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

SIG_LONG_EDGE = 128
"""Long edge, in cells, of the signature grid (~1 cell per 15px on a 1920px stream)."""


@dataclass(frozen=True)
class Frame:
    seq: int
    rgb: np.ndarray  # H x W x 3, uint8
    captured_at: float  # time.time() of the pixels
    changed_at: float  # time.time() when this seq was issued

    @property
    def width(self) -> int:
        return int(self.rgb.shape[1])

    @property
    def height(self) -> int:
        return int(self.rgb.shape[0])


@dataclass
class ChangeConfig:
    cell_threshold: float = 6.0
    """Mean-abs-diff (0-255) above which a signature cell counts as changed."""
    min_changed_cells: int = 2
    """Number of unmasked cells that must change for a new seq (a typed digit is ~2-6)."""
    ignore_top_portrait: float = 0.06
    """Fraction of height masked at the top in portrait (iOS status bar)."""
    ignore_regions: list[tuple[float, float, float, float]] = field(default_factory=list)
    """Extra normalized (x, y, w, h) regions excluded from change detection."""
    history: int = 64
    """How many past signatures to keep for ``changes since seq`` crops."""


def signature(rgb: np.ndarray) -> np.ndarray:
    """Downsample to a coarse grayscale grid of block means (float32)."""
    h, w = rgb.shape[:2]
    scale = SIG_LONG_EDGE / max(h, w)
    gh, gw = max(1, round(h * scale)), max(1, round(w * scale))
    gray = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    # Trim so the image divides evenly into blocks, then average each block.
    bh, bw = max(1, h // gh), max(1, w // gw)
    gray = gray[: gh * bh, : gw * bw]
    return gray.reshape(gh, bh, gw, bw).mean(axis=(1, 3), dtype=np.float32)


class FrameStore:
    """Thread-safe holder of the latest frame plus change bookkeeping."""

    def __init__(self, config: ChangeConfig | None = None) -> None:
        self.config = config or ChangeConfig()
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._seq = 0
        self._sig_ref: np.ndarray | None = None  # signature of the last seq'd frame
        self._sig_prev: np.ndarray | None = None  # signature of the last submitted frame
        self._sig_history: OrderedDict[int, np.ndarray] = OrderedDict()
        self._last_motion = 0.0
        self._frames_submitted = 0

    # ----------------------------------------------------------------- writer
    def submit(self, rgb: np.ndarray, captured_at: float | None = None) -> bool:
        """Offer a new decoded frame. Returns True if it produced a new seq."""
        now = captured_at if captured_at is not None else time.time()
        sig = signature(rgb)
        with self._cond:
            self._frames_submitted += 1
            same_shape = self._sig_ref is not None and self._sig_ref.shape == sig.shape
            mask = self._mask(sig.shape, rgb.shape[0] > rgb.shape[1])

            if self._sig_prev is None or self._sig_prev.shape != sig.shape:
                self._last_motion = now
            elif self._changed_cells(self._sig_prev, sig, mask).any():
                self._last_motion = now
            self._sig_prev = sig

            significant = not same_shape or self._significant(self._sig_ref, sig, mask)
            if significant:
                self._seq += 1
                self._sig_ref = sig
                self._sig_history[self._seq] = sig
                while len(self._sig_history) > self.config.history:
                    self._sig_history.popitem(last=False)
                self._frame = Frame(self._seq, rgb, now, now)
            else:
                # Keep the freshest pixels (e.g. clock ticked) under the same seq.
                assert self._frame is not None
                self._frame = Frame(self._seq, rgb, now, self._frame.changed_at)
            self._cond.notify_all()
            return significant

    # ----------------------------------------------------------------- readers
    @property
    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    @property
    def last_motion(self) -> float:
        with self._cond:
            return self._last_motion

    @property
    def frames_submitted(self) -> int:
        with self._cond:
            return self._frames_submitted

    def wait_for_seq_after(self, seq: int, timeout: float) -> Frame | None:
        """Block until a frame with ``seq`` greater than the given one exists."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._frame is None or self._frame.seq <= seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._frame

    def wait_settled(self, settle: float, timeout: float) -> Frame | None:
        """Wait until no motion for ``settle`` seconds (bounded by ``timeout``).

        iOS only sends frames while the screen changes, so "no new motion for N
        ms" is a reliable signal that animations have finished.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                quiet_for = time.time() - self._last_motion
                remaining = deadline - time.monotonic()
                if quiet_for >= settle or remaining <= 0:
                    return self._frame
                self._cond.wait(min(settle - quiet_for, remaining))

    def changed_bbox(self, since_seq: int) -> tuple[float, float, float, float] | None:
        """Normalized (x, y, w, h) box around cells changed since ``since_seq``.

        Returns None when the reference signature is no longer in history (or
        the resolution changed); callers should fall back to a full frame.
        Returns (0, 0, 0, 0) when nothing outside the masks changed.
        """
        with self._cond:
            old = self._sig_history.get(since_seq)
            cur = self._sig_ref
            frame = self._frame
        if old is None or cur is None or frame is None or old.shape != cur.shape:
            return None
        mask = self._mask(cur.shape, frame.height > frame.width)
        changed = self._changed_cells(old, cur, mask)
        if not changed.any():
            return (0.0, 0.0, 0.0, 0.0)
        rows = np.flatnonzero(changed.any(axis=1))
        cols = np.flatnonzero(changed.any(axis=0))
        gh, gw = cur.shape
        # Pad by one cell so the crop has context around the change.
        r0, r1 = max(0, rows[0] - 1), min(gh, rows[-1] + 2)
        c0, c1 = max(0, cols[0] - 1), min(gw, cols[-1] + 2)
        return (c0 / gw, r0 / gh, (c1 - c0) / gw, (r1 - r0) / gh)

    # ---------------------------------------------------------------- helpers
    def _mask(self, shape: tuple[int, ...], portrait: bool) -> np.ndarray:
        """Boolean grid: True where changes count."""
        gh, gw = shape[:2]
        mask = np.ones((gh, gw), dtype=bool)
        if portrait and self.config.ignore_top_portrait > 0:
            mask[: int(round(gh * self.config.ignore_top_portrait))] = False
        for x, y, w, h in self.config.ignore_regions:
            mask[int(y * gh) : int(np.ceil((y + h) * gh)), int(x * gw) : int(np.ceil((x + w) * gw))] = False
        return mask

    def _changed_cells(self, a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> np.ndarray:
        return (np.abs(a - b) > self.config.cell_threshold) & mask

    def _significant(self, ref: np.ndarray | None, sig: np.ndarray, mask: np.ndarray) -> bool:
        if ref is None:
            return True
        return int(self._changed_cells(ref, sig, mask).sum()) >= self.config.min_changed_cells
