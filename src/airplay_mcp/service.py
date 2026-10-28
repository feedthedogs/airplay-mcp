"""Transport-independent screen logic behind the MCP tools.

Everything here is designed so the model pays for pixels only when they carry
new information: per-client dedupe, changed-region crops, letterbox removal,
right-sized images, and settle-before-capture.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image

from . import imaging
from .frames import Frame, FrameStore
from .imaging import Region
from .receivers import ReceiverStatus


@dataclass
class ScreenResult:
    text: str
    image: bytes | None = None
    mime: str = "image/jpeg"


MIN_CHANGE_CROP = 256
"""Minimum edge (screen px) of a ``mode='changes'`` crop."""


def _ago(t: float) -> str:
    s = max(0.0, time.time() - t)
    return f"{s:.1f}s ago" if s < 60 else f"{s / 60:.1f}min ago"


class ScreenService:
    def __init__(
        self,
        store: FrameStore,
        receiver_status: Callable[[], ReceiverStatus],
        streaming: Callable[[], bool],
        default_max_dim: int = 1024,
        jpeg_quality: int = 80,
        video_stats: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self.store = store
        self.receiver_status = receiver_status
        self.streaming = streaming
        self.video_stats = video_stats  # () -> (bytes received, frames decoded)
        self.default_max_dim = default_max_dim
        self.jpeg_quality = jpeg_quality
        self._delivered: OrderedDict[object, int] = OrderedDict()
        self._box_cache: tuple[int, tuple[int, int, int, int]] | None = None

    # ---------------------------------------------------------- bookkeeping
    def last_delivered(self, client: object) -> int | None:
        return self._delivered.get(client)

    def _mark_delivered(self, client: object, seq: int) -> None:
        self._delivered[client] = seq
        self._delivered.move_to_end(client)
        while len(self._delivered) > 256:
            self._delivered.popitem(last=False)

    def _screen_box(self, frame: Frame) -> tuple[int, int, int, int]:
        """Letterbox-free pixel box, cached per seq (bars don't change within a seq)."""
        if self._box_cache is None or self._box_cache[0] != frame.seq:
            self._box_cache = (frame.seq, imaging.content_box(frame.rgb))
        return self._box_cache[1]

    def _no_screen_text(self) -> str:
        st = self.receiver_status()
        if not st.running:
            if st.detail and st.detail != "stopped":
                return f"No screen available. Receiver problem: {st.detail}"
            return "No screen available: the AirPlay receiver is off. Call start_receiver first."
        if st.client:
            return (
                f"No screen yet: {st.client} is connected but no picture has decoded"
                f" ({self._video_note()}). If this persists, check the server log; the user"
                " can also stop and restart Screen Mirroring."
            )
        return (
            f"No screen yet. On the iPhone/iPad open Control Centre > Screen Mirroring and pick "
            f"'{st.advertised_name}' (same Wi-Fi network)."
        )

    # ---------------------------------------------------------------- tools
    def status(self, client: object) -> str:
        st = self.receiver_status()
        frame = self.store.latest
        parts = [
            f"receiver: on as '{st.advertised_name}'" if st.running else "receiver: off (call start_receiver)",
            f"mirroring: {'yes' if self.streaming() else 'no'}",
        ]
        if st.client:
            parts.append(f"device: {st.client}")
        if frame:
            x0, y0, x1, y1 = self._screen_box(frame)
            w, h = x1 - x0, y1 - y0
            parts += [
                f"frame: {frame.seq}",
                f"screen: {w}x{h} {'portrait' if h >= w else 'landscape'}",
                f"last change: {_ago(frame.changed_at)}",
            ]
            seen = self.last_delivered(client)
            if seen is not None:
                parts.append("you have seen the current frame" if seen == frame.seq else f"you last saw frame {seen}")
        elif st.client:
            parts.append(f"video: {self._video_note()}")
        if st.detail and (not st.running or not frame):
            parts.append(f"note: {st.detail}")
        return " | ".join(parts)

    def _video_note(self) -> str:
        if self.video_stats is None:
            return "no frames decoded"
        received, decoded = self.video_stats()
        return f"{received / 1024:.0f} KB received, {decoded} frames decoded"

    def get_screen(
        self,
        client: object,
        mode: Literal["full", "changes"] = "full",
        max_dim: int | None = None,
        region: Region | None = None,
        since_frame: int | None = None,
        force: bool = False,
        settle_ms: int = 300,
        grid: tuple[int, int] | None = None,
        fmt: Literal["jpeg", "png"] = "jpeg",
    ) -> ScreenResult:
        if self.store.latest is None:
            self.store.wait_for_seq_after(0, timeout=2.0)
        if settle_ms > 0:
            self.store.wait_settled(settle_ms / 1000, timeout=max(2.0, settle_ms / 1000 * 4))
        frame = self.store.latest
        if frame is None:
            return ScreenResult(self._no_screen_text())

        baseline = since_frame if since_frame is not None else self.last_delivered(client)
        # Only skip the image when the caller says which frame it holds. The server can't
        # know whether the model still has an earlier image (context reset, new session),
        # so a bare get_screen always returns the current frame.
        if not force and region is None and since_frame is not None and since_frame == frame.seq:
            return ScreenResult(
                f"unchanged: still frame {frame.seq} (last change {_ago(frame.changed_at)}). "
                "Reuse the image you already have; pass force=true to resend."
            )

        sx0, sy0, sx1, sy1 = self._screen_box(frame)
        sw, sh = sx1 - sx0, sy1 - sy0
        note = ""
        if mode == "changes" and region is None and baseline is not None and baseline != frame.seq:
            bbox = self.store.changed_bbox(baseline)
            if bbox is None:
                note = f"frame {baseline} is too old to diff; sending full screen. "
            elif bbox == (0.0, 0.0, 0.0, 0.0):
                self._mark_delivered(client, frame.seq)
                return ScreenResult(
                    f"frame {frame.seq}: only ignored areas (e.g. status bar) changed since frame {baseline}."
                )
            else:
                region = self._full_to_screen(bbox, frame, (sx0, sy0, sx1, sy1))
                note = f"changes since frame {baseline} only. "

        screen = Image.fromarray(frame.rgb[sy0:sy1, sx0:sx1])
        img, (px0, py0, px1, py1) = imaging.crop_region(screen, region)
        img = imaging.fit(img, max_dim or self.default_max_dim)
        if grid:
            img = imaging.draw_grid(img, *grid)
        data = imaging.encode(img, fmt, self.jpeg_quality)
        self._mark_delivered(client, frame.seq)

        w, h = img.size
        text = f"frame {frame.seq} | {note}screen {sw}x{sh}"
        if region is not None:
            text += (
                f" | region x={px0 / sw:.3f} y={py0 / sh:.3f} w={(px1 - px0) / sw:.3f} h={(py1 - py0) / sh:.3f}"
                " (fractions of the screen)"
            )
        text += f" | image {w}x{h} (~{imaging.estimate_tokens(w, h)} tokens) | changed {_ago(frame.changed_at)}"
        if not self.streaming():
            text += " | mirroring has stopped: this is the last frame received"
        if grid:
            text += f" | grid {grid[0]}x{grid[1]}: columns A.., rows 1.."
        return ScreenResult(text, data, f"image/{fmt}")

    def wait_for_change(self, client: object, timeout_s: float = 30, since_frame: int | None = None) -> str:
        cur = self.store.latest
        baseline = since_frame if since_frame is not None else self.last_delivered(client)
        if baseline is None:
            baseline = cur.seq if cur else 0
        if cur is not None and cur.seq > baseline:
            return f"changed: frame {cur.seq} is newer than frame {baseline}. Call get_screen to see it."
        frame = self.store.wait_for_seq_after(baseline, timeout=timeout_s)
        if frame is None:
            if self.store.latest is None:
                return f"no change within {timeout_s:g}s. {self._no_screen_text()}"
            return f"no change within {timeout_s:g}s (still frame {baseline})."
        self.store.wait_settled(0.3, timeout=3)
        frame = self.store.latest or frame
        return f"changed: now frame {frame.seq} (was {baseline}). Call get_screen (mode='changes' for just the changed area)."

    def save_screen(
        self,
        path: str,
        region: Region | None = None,
        max_dim: int | None = None,
        fmt: Literal["jpeg", "png"] | None = None,
    ) -> str:
        """Write the current screen to disk at full resolution (unless ``max_dim``)."""
        got = self.screen_for_ocr(region)
        if got is None:
            return self._no_screen_text()
        frame, img = got
        if max_dim:
            img = imaging.fit(img, max_dim)
        out = Path(path).expanduser().resolve()
        if fmt is None:
            fmt = "jpeg" if out.suffix.lower() in (".jpg", ".jpeg") else "png"
        if not out.suffix:
            out = out.with_suffix(".jpg" if fmt == "jpeg" else ".png")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(imaging.encode(img, fmt, self.jpeg_quality))
        return f"saved frame {frame.seq} ({img.width}x{img.height} {fmt}) to {out}"

    def screen_for_ocr(self, region: Region | None) -> tuple[Frame, Image.Image] | None:
        frame = self.store.latest
        if frame is None:
            return None
        x0, y0, x1, y1 = self._screen_box(frame)
        img, _ = imaging.crop_region(Image.fromarray(frame.rgb[y0:y1, x0:x1]), region)
        return frame, img

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _full_to_screen(
        bbox: Region, frame: Frame, box: tuple[int, int, int, int]
    ) -> Region:
        """Convert a normalized full-frame box into normalized screen (letterbox-free) coords."""
        x0, y0, x1, y1 = box
        sw, sh = x1 - x0, y1 - y0
        bx, by, bw, bh = bbox
        px0, py0 = bx * frame.width, by * frame.height
        px1, py1 = px0 + bw * frame.width, py0 + bh * frame.height
        px0, px1 = min(max(px0, x0), x1), min(max(px1, x0), x1)
        py0, py1 = min(max(py0, y0), y1), min(max(py1, y0), y1)
        # Grow tiny boxes so the crop shows enough surroundings to be interpretable.
        px0, px1 = _grow(px0, px1, MIN_CHANGE_CROP, x0, x1)
        py0, py1 = _grow(py0, py1, MIN_CHANGE_CROP, y0, y1)
        return ((px0 - x0) / sw, (py0 - y0) / sh, (px1 - px0) / sw, (py1 - py0) / sh)


def _grow(a: float, b: float, size: float, lo: float, hi: float) -> tuple[float, float]:
    """Expand [a, b] around its centre to at least ``size``, kept within [lo, hi]."""
    size = min(size, hi - lo)
    if b - a >= size:
        return a, b
    c = (a + b) / 2
    a = min(max(c - size / 2, lo), hi - size)
    return a, a + size
