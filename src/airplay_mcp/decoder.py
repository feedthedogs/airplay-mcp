"""Decode the mirrored H.264/H.265 stream into the FrameStore.

Two inputs feed the same decoder:

* **Whole access units** from the native receiver (``begin_stream`` /
  ``submit_au`` / ``end_stream``). Each AU is decoded as one packet, so a frame
  is available the moment its AU arrives.
* **A raw Annex-B byte stream over local TCP** (``--external``), e.g.::

      ffmpeg -re -i clip.mp4 -an -c:v libx264 -f h264 tcp://127.0.0.1:PORT

  A byte stream has no AU boundaries, so the parser holds back the newest frame
  until the next one starts; it is flushed whenever the stream goes idle.

iOS only sends frames while the screen changes, so the *last* frame of a burst
is the one that matters: it must reach the store promptly, not when the user
next touches the phone. Every packet is decoded (inter-frame prediction), but
RGB conversion and change detection run at most ``sample_fps`` times per second
during motion, plus once as soon as the stream has been idle for ``idle_flush``
seconds, so the store converges on what is actually on screen.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from collections.abc import Callable

import av
import numpy as np

from .frames import FrameStore

log = logging.getLogger(__name__)

IDLE_FLUSH = 0.05
"""Seconds without new video after which the newest decoded frame is stored."""

_BEGIN, _AU, _END = 0, 1, 2


def luma_of(frame: av.VideoFrame) -> np.ndarray | None:
    """Zero-copy view of the luma plane, rescaled lazily by the caller; None if not planar YUV."""
    if not frame.format.name.startswith(("yuv", "yuvj", "nv12")):
        return None
    p = frame.planes[0]
    return np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)[:, : p.width]


class _Sampler:
    """Decodes packets and decides which decoded frames go to the store."""

    def __init__(self, owner: StreamDecoder) -> None:
        self.owner = owner
        self.ctx = av.CodecContext.create(owner.codec, "r")
        self.pending: av.VideoFrame | None = None
        self.last_sample = 0.0

    def decode(self, packet: av.Packet) -> None:
        o = self.owner
        try:
            frames = self.ctx.decode(packet)
        except av.error.InvalidDataError as e:
            # Normal until SPS/PPS + a keyframe arrive; persistent errors mean a
            # corrupt stream (e.g. bad decryption upstream), so don't stay silent.
            o.decode_errors += 1
            if o.decode_errors in (1, 50) or o.decode_errors % 1000 == 0:
                log.warning("undecodable H.264 packet #%d (%d B): %s", o.decode_errors, packet.size, e)
            return
        for frame in frames:
            o.frames_decoded += 1
            if time.monotonic() - self.last_sample >= o.sample_interval:
                self.sample(frame)
            else:
                self.pending = frame

    def flush(self) -> None:
        """Stream went quiet: make sure the newest frame is stored."""
        if self.pending is not None:
            self.sample(self.pending)

    def sample(self, frame: av.VideoFrame) -> None:
        self.pending = None
        self.last_sample = time.monotonic()
        rgb = frame.to_ndarray(format="rgb24")
        luma = luma_of(frame)
        full_range = frame.format.name.startswith("yuvj") or getattr(frame, "color_range", 0) == 2
        self.owner.store.submit(rgb, luma=luma, luma_full_range=full_range)


class StreamDecoder:
    def __init__(
        self,
        store: FrameStore,
        host: str = "127.0.0.1",
        port: int = 0,
        codec: str = "h264",
        sample_fps: float = 4.0,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        idle_flush: float = IDLE_FLUSH,
    ) -> None:
        self.store = store
        self.codec = codec
        self.sample_interval = 1.0 / sample_fps
        self.idle_flush = idle_flush
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.streaming = False
        self.bytes_received = 0
        self.frames_decoded = 0
        self.decode_errors = 0
        self._stop = threading.Event()
        self._sock = socket.create_server((host, port))
        self._sock.settimeout(0.5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="stream-decoder", daemon=True)
        self._aus: queue.SimpleQueue[tuple[int, bytes | None]] = queue.SimpleQueue()
        self._au_thread = threading.Thread(target=self._decode_aus, name="au-decoder", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._au_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()
        self._aus.put((_END, None))
        self._thread.join(timeout=2)
        self._au_thread.join(timeout=2)

    # ------------------------------------------------- access units (native)
    # Thread-safe and non-blocking: called from the receiver's event loop.
    def begin_stream(self) -> None:
        self._aus.put((_BEGIN, None))

    def submit_au(self, data: bytes) -> None:
        self._aus.put((_AU, data))

    def end_stream(self) -> None:
        self._aus.put((_END, None))

    def _decode_aus(self) -> None:
        sampler: _Sampler | None = None
        while not self._stop.is_set():
            try:
                kind, data = self._aus.get(timeout=self.idle_flush if sampler and sampler.pending else 0.5)
            except queue.Empty:
                if sampler is not None:
                    sampler.flush()
                continue
            if kind == _AU:
                if sampler is None:  # AUs without begin_stream: start implicitly
                    sampler = self._open_sampler()
                assert data is not None
                self.bytes_received += len(data)
                try:
                    sampler.decode(av.Packet(data))
                except Exception:
                    log.exception("decoder error")
            elif kind == _BEGIN:
                self._close_sampler(sampler)
                sampler = self._open_sampler()
            else:
                self._close_sampler(sampler)
                sampler = None
        self._close_sampler(sampler)

    def _open_sampler(self) -> _Sampler:
        log.info("video stream started")
        self.streaming = True
        if self.on_connect:
            self.on_connect()
        return _Sampler(self)

    def _close_sampler(self, sampler: _Sampler | None) -> None:
        if sampler is None:
            return
        sampler.flush()
        self.streaming = False
        log.info("video stream ended")
        if self.on_disconnect:
            self.on_disconnect()

    # ---------------------------------------------- byte stream (--external)
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            log.info("video stream connected from %s", addr)
            sampler = self._open_sampler()
            try:
                self._decode_connection(conn, sampler)
            except Exception:
                log.exception("decoder error")
            finally:
                conn.close()
                self._close_sampler(sampler)

    def _decode_connection(self, conn: socket.socket, sampler: _Sampler) -> None:
        conn.settimeout(self.idle_flush)
        parsed_since_flush = False
        while not self._stop.is_set():
            try:
                data = conn.recv(1 << 16)
            except TimeoutError:
                # Idle: release the frame the parser is holding, then store it.
                if parsed_since_flush:
                    for packet in sampler.ctx.parse(None):
                        sampler.decode(packet)
                    parsed_since_flush = False
                sampler.flush()
                continue
            except ConnectionError:  # sender killed or reset the socket: same as a clean end
                break
            if not data:
                break
            self.bytes_received += len(data)
            for packet in sampler.ctx.parse(data):
                sampler.decode(packet)
            parsed_since_flush = True
        for packet in sampler.ctx.parse(None):
            sampler.decode(packet)
