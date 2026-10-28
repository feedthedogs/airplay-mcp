"""End-to-end tool flow over an in-process MCP client, fed by a synthetic H.264 stream."""
import socket
import threading

import anyio
import av
import numpy as np
import pytest
from mcp import Client

from airplay_mcp.server import build_server, parse_args


def _encode_frames(n: int = 30, w: int = 320, h: int = 640) -> bytes:
    """A short portrait clip whose content changes halfway through, as Annex-B H.264."""
    ctx = av.CodecContext.create("libx264", "w")
    ctx.width, ctx.height, ctx.pix_fmt = w, h, "yuv420p"
    ctx.options = {"tune": "zerolatency", "preset": "ultrafast"}
    out = b""
    for i in range(n):
        img = np.full((h, w, 3), 40, np.uint8)
        if i >= n // 2:
            img[200:400, 60:260] = (230, 60, 60)
        for pkt in ctx.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            out += bytes(pkt)
    for pkt in ctx.encode(None):
        out += bytes(pkt)
    return out


def _text(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


@pytest.mark.anyio
async def test_start_wait_get_save_stop(tmp_path):
    args = parse_args(["--external", "--save-dir", str(tmp_path)])
    server = build_server(args)
    stream = _encode_frames()
    async with Client(server) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        assert {"start_receiver", "stop_receiver", "wait_for_change", "get_screen", "save_screen"} <= tools

        assert "receiver: off" in _text(await client.call_tool("screen_status", {}))
        started = _text(await client.call_tool("start_receiver", {}))
        port = int(started.rsplit(":", 1)[1].rstrip("."))

        def send() -> None:
            with socket.create_connection(("127.0.0.1", port)) as s:
                s.sendall(stream)
                threading.Event().wait(1.5)  # keep the connection up while frames are analysed

        sender = threading.Thread(target=send)
        sender.start()
        waited = _text(await client.call_tool("wait_for_change", {"timeout_s": 10}))
        assert waited.startswith("changed"), waited

        shot = await client.call_tool("get_screen", {"settle_ms": 500})
        assert any(c.type == "image" for c in shot.content)

        saved = _text(await client.call_tool("save_screen", {"path": "shot.png"}))
        assert (tmp_path / "shot.png").exists(), saved
        assert "320x640" in saved

        stopped = _text(await client.call_tool("stop_receiver", {}))
        assert "off" in stopped
        await anyio.to_thread.run_sync(sender.join)


@pytest.fixture
def anyio_backend():
    return "asyncio"
