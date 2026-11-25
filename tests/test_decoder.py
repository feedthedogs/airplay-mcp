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


def _access_units(frames: int, change_at: int, w: int = 320, h: int = 640) -> list[bytes]:
    """One Annex-B access unit per frame, as pylibairplay delivers them (no B-frames)."""
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.options = {"tune": "zerolatency", "preset": "veryfast"}
    import fractions

    enc.time_base = fractions.Fraction(1, 30)
    aus = []
    for i in range(frames):
        img = np.full((h, w, 3), 230, dtype=np.uint8)
        if i >= change_at:
            img[300:360, 100:160] = 10
        f = av.VideoFrame.from_ndarray(img, format="rgb24")
        f.pts = i
        aus += [bytes(p) for p in enc.encode(f)]
    aus += [bytes(p) for p in enc.encode(None)]
    return aus


def test_last_access_unit_is_shown_without_waiting_for_another() -> None:
    """iOS sends nothing while the screen is still, so the final frame of a change
    must reach the store on its own (a byte-stream parser would hold it back)."""
    store = FrameStore()
    dec = StreamDecoder(store, sample_fps=50)
    dec.start()
    try:
        aus = _access_units(6, change_at=5)  # only the very last frame shows the change
        dec.begin_stream()
        for au in aus:
            dec.submit_au(au)
        frame = store.wait_for_seq_after(1, timeout=2)
        assert frame is not None and frame.rgb[330, 130].mean() < 60
        assert dec.frames_decoded == len(aus)
    finally:
        dec.stop()


def test_idle_byte_stream_flushes_the_held_back_frame() -> None:
    store = FrameStore()
    dec = StreamDecoder(store, sample_fps=50)
    dec.start()
    try:
        data = b"".join(_access_units(6, change_at=5))
        with socket.create_connection(("127.0.0.1", dec.port)) as s:
            s.sendall(data)  # and keep the connection open, like an idle mirror stream
            frame = store.wait_for_seq_after(1, timeout=2)
            assert frame is not None and frame.rgb[330, 130].mean() < 60
    finally:
        dec.stop()
