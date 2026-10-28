"""Receive an Annex-B H.264/H.265 byte stream over local TCP and decode it.

The native receiver (receivers/native.py) decrypts the AirPlay mirror stream and
forwards the Annex-B access units to this port. Anything else that can emit Annex-B
over TCP works too (``--external``), e.g.::

    ffmpeg -re -i clip.mp4 -an -c:v libx264 -f h264 tcp://127.0.0.1:PORT

Every packet must be decoded (inter-frame prediction), but RGB conversion and
change detection run at most ``sample_fps`` times per second, always
including the last frame of a burst so the store converges on what is
actually on screen.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

import av

from .frames import FrameStore

log = logging.getLogger(__name__)


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
    ) -> None:
        self.store = store
        self.codec = codec
        self.sample_interval = 1.0 / sample_fps
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

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            log.info("video stream connected from %s", addr)
            self.streaming = True
            if self.on_connect:
                self.on_connect()
            try:
                self._decode_connection(conn)
            except Exception:
                log.exception("decoder error")
            finally:
                conn.close()
                self.streaming = False
                log.info("video stream ended")
                if self.on_disconnect:
                    self.on_disconnect()

    def _decode_connection(self, conn: socket.socket) -> None:
        ctx = av.CodecContext.create(self.codec, "r")
        conn.settimeout(self.sample_interval)
        pending: av.VideoFrame | None = None
        last_sample = 0.0

        def sample(frame: av.VideoFrame) -> None:
            nonlocal last_sample
            last_sample = time.monotonic()
            self.store.submit(frame.to_ndarray(format="rgb24"))

        while not self._stop.is_set():
            try:
                data = conn.recv(1 << 16)
            except TimeoutError:
                # Stream went quiet (static screen): make sure the newest frame is stored.
                if pending is not None:
                    sample(pending)
                    pending = None
                continue
            except ConnectionError:  # sender killed or reset the socket: same as a clean end
                break
            if not data:
                break
            self.bytes_received += len(data)
            for packet in ctx.parse(data):
                try:
                    frames = ctx.decode(packet)
                except av.error.InvalidDataError as e:
                    # Normal until SPS/PPS + a keyframe arrive; persistent errors mean a
                    # corrupt stream (e.g. bad decryption upstream), so don't stay silent.
                    self.decode_errors += 1
                    if self.decode_errors in (1, 50) or self.decode_errors % 1000 == 0:
                        log.warning("undecodable H.264 packet #%d (%d B): %s",
                                    self.decode_errors, packet.size, e)
                    continue
                for frame in frames:
                    self.frames_decoded += 1
                    if time.monotonic() - last_sample >= self.sample_interval:
                        sample(frame)
                        pending = None
                    else:
                        pending = frame
        if pending is not None:
            sample(pending)
