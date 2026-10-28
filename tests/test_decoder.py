"""Decode a real H.264 Annex-B stream pushed over TCP, as the native receiver does."""

import io
import socket
import time

import av
import numpy as np

from airplay_mcp.decoder import StreamDecoder
from airplay_mcp.frames import FrameStore


def make_h264(frames: int = 20, w: int = 320, h: int = 640) -> bytes:
    buf = io.BytesIO()
    with av.open(buf, "w", format="h264") as out:
        stream = out.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
        for i in range(frames):
            img = np.full((h, w, 3), 230, dtype=np.uint8)
            if i >= frames // 2:  # the "screen" changes half way
                img[300:360, 100:160] = 10
            for packet in stream.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    return buf.getvalue()


def test_stream_is_decoded_into_store() -> None:
    store = FrameStore()
    dec = StreamDecoder(store, sample_fps=50)
    dec.start()
    try:
        data = make_h264()
        with socket.create_connection(("127.0.0.1", dec.port)) as s:
            for i in range(0, len(data), 4096):
                s.sendall(data[i : i + 4096])
            deadline = time.time() + 5
            while store.seq < 2 and time.time() < deadline:
                time.sleep(0.05)
        assert dec.frames_decoded > 0
        frame = store.latest
        assert frame is not None and (frame.width, frame.height) == (320, 640)
        assert store.seq >= 2  # the mid-stream change was detected
        assert frame.rgb[330, 130].mean() < 60  # and the newest pixels are stored
    finally:
        dec.stop()
