"""MCP server: exposes the mirrored phone screen with token-frugal tools.

Every option can also be set with an AIRPLAY_* environment variable (e.g.
AIRPLAY_NAME, AIRPLAY_FP_EMU_DIR), which is handy in MCP client JSON configs.

The receiver defaults to the in-process pylibairplay one. ``--receiver uxplay`` and
``--receiver gstreamer`` are opt-in alternatives that need external programs.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from collections.abc import AsyncIterator
from typing import Annotated, Literal

import anyio
import mcp.types as types
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus, ResourceUpdated
from PIL import Image as PILImage
from pydantic import Field

from . import imaging, ocr
from .decoder import StreamDecoder
from .frames import ChangeConfig, FrameStore
from .receivers import GStreamerReceiver, NativeReceiver, Receiver, ReceiverStatus, UxPlayReceiver
from .service import ScreenService

log = logging.getLogger("airplay_mcp")

SCREEN_URI = "airplay://screen/latest"

INSTRUCTIONS = """\
Gives you the screen of a phone/tablet that is AirPlay-mirroring to this server.
Typical flow:
1. start_receiver: the server appears as an AirPlay target on the local network.
   Tell the user to open Control Centre > Screen Mirroring on their iPhone/iPad and
   pick the name it reports (same Wi-Fi network).
2. wait_for_change: blocks until the first frame arrives (or the screen next changes).
3. get_screen to look at it, or save_screen to write it to disk for other tools.
4. Repeat 2-3 as the user acts on the phone; stop_receiver when you are done.
To keep token use low:
- Use screen_status (text only) to check connection or whether anything changed.
- Every get_screen reply starts 'frame N'. Remember N: it is the frame you hold.
- get_screen always returns the current frame, even if the screen hasn't changed, so
  call it whenever the user asks you to look (e.g. 'have another look'): the screen
  may be static because they are still on the same clue. Pass since_frame=N only when
  you still hold frame N in context, to get a short 'unchanged' text instead.
- wait_for_change only fires when the screen changes. Always pass since_frame=N (the
  frame you hold). If you have lost your last image (new session, long gap), call
  get_screen instead of waiting.
- Prefer mode='changes' after the user acts on the phone: you get only the changed area.
- To read small detail, request a `region` at full detail instead of a bigger whole screen.
- Use wait_for_change to block until the user does something instead of polling.
- For text-heavy screens, read_screen_text (if available) is much cheaper than an image.
"""


class NullReceiver(Receiver):
    """Used with --receiver external: something else pushes Annex-B H.264 to our port, e.g.
    ``ffmpeg -re -i clip.mp4 -an -c:v libx264 -f h264 tcp://127.0.0.1:PORT``."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.running = False

    def start(self) -> None:
        self.running = True
        log.info("waiting for an external stream on tcp://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        self.running = False

    def status(self) -> ReceiverStatus:
        return ReceiverStatus(
            self.running,
            "external source",
            f"listening on tcp://127.0.0.1:{self.port} for raw H.264",
        )


def build_server(args: argparse.Namespace) -> MCPServer:
    store = FrameStore(
        ChangeConfig(
            ignore_top_portrait=args.ignore_top,
            min_changed_cells=args.min_changed_cells,
        )
    )
    decoder = StreamDecoder(
        store,
        port=args.stream_port,
        codec=args.codec,
        sample_fps=args.sample_fps,
    )
    receiver: Receiver
    if args.receiver == "external":
        receiver = NullReceiver(decoder.port)
    elif args.receiver == "uxplay":
        receiver = UxPlayReceiver(
            decoder.port, name=args.name, uxplay_path=args.uxplay_path, fps=args.fps, extra_args=args.uxplay_arg
        )
    elif args.receiver == "gstreamer":
        receiver = GStreamerReceiver(decoder.port, args.gst_pipeline, args.gst_path)
    else:
        receiver = NativeReceiver(
            decoder.port, name=args.name, fp_emu_dir=args.fp_emu_dir,
            fp_emu_python=args.fp_emu_python, pin=args.pin,
        )
    service = ScreenService(
        store, receiver.status, lambda: decoder.streaming, default_max_dim=args.max_dim, jpeg_quality=args.quality,
        video_stats=lambda: (decoder.bytes_received, decoder.frames_decoded),
    )
    bus = InMemorySubscriptionBus()
    legacy_subscribers: set[object] = set()

    async def notify_changes() -> None:
        """Debounced resource-updated notifications once the screen settles on a new seq."""
        announced = 0
        while True:
            await anyio.sleep(0.5)
            seq = store.seq
            if seq == announced or time.time() - store.last_motion < 0.3:  # still moving
                continue
            announced = seq
            await bus.publish(ResourceUpdated(uri=SCREEN_URI))
            for session in list(legacy_subscribers):
                try:
                    await session.send_resource_updated(SCREEN_URI)  # type: ignore[attr-defined]
                except Exception:
                    legacy_subscribers.discard(session)

    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        decoder.start()
        if args.autostart:
            receiver.start()
        async with anyio.create_task_group() as tg:
            tg.start_soon(notify_changes)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
                await anyio.to_thread.run_sync(receiver.stop)
                decoder.stop()

    server = MCPServer(
        name="airplay-screen",
        title="AirPlay Screen",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        subscriptions=bus,
    )

    # Pre-2026 clients subscribe with resources/subscribe; newer ones use subscriptions/listen (the bus).
    async def on_subscribe(ctx, params: types.SubscribeRequestParams) -> types.EmptyResult:  # type: ignore[no-untyped-def]
        if str(params.uri) == SCREEN_URI:
            legacy_subscribers.add(ctx.session)
        return types.EmptyResult()

    async def on_unsubscribe(ctx, params: types.UnsubscribeRequestParams) -> types.EmptyResult:  # type: ignore[no-untyped-def]
        legacy_subscribers.discard(ctx.session)
        return types.EmptyResult()

    lowlevel = server._lowlevel_server  # no public hook for legacy subscribe handlers
    lowlevel.add_request_handler("resources/subscribe", types.SubscribeRequestParams, on_subscribe)
    lowlevel.add_request_handler("resources/unsubscribe", types.UnsubscribeRequestParams, on_unsubscribe)

    read_only = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
    control = types.ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=control, structured_output=False)
    async def start_receiver() -> str:
        """Turn the AirPlay receiver on so a phone/tablet can mirror to it. Returns what
        to tell the user. Safe to call when already on. Follow with wait_for_change."""
        already = receiver.status().running
        await anyio.to_thread.run_sync(receiver.start)
        if args.receiver == "external":
            return f"Accepting a raw H.264 stream on tcp://127.0.0.1:{decoder.port}."
        if args.receiver == "gstreamer":
            return f"GStreamer source: {receiver.status().detail}. Then call wait_for_change to wait for the first frame."
        lines = [
            "AirPlay receiver is already on." if already else "AirPlay receiver is on.",
            f"Ask the user to open Control Centre > Screen Mirroring on their iPhone/iPad and pick "
            f"'{args.name}' (same Wi-Fi network as this computer). If asked for a code, it is {args.pin}.",
        ]
        if args.receiver == "uxplay":
            st = receiver.status()
            if not st.running and st.detail:
                lines.append(f"Problem: {st.detail}")
        elif args.fp_emu_dir and not already:
            lines.append("The FairPlay emulator takes ~35s to warm up; mirroring works after that.")
        elif not args.fp_emu_dir:
            lines.append(
                "Note: FairPlay is not configured (AIRPLAY_FP_EMU_DIR). Recent iOS versions need it, "
                "so the phone may connect without any video arriving."
            )
        lines.append("Then call wait_for_change to wait for the first frame.")
        return "\n".join(lines)

    @server.tool(annotations=control, structured_output=False)
    async def stop_receiver() -> str:
        """Turn the AirPlay receiver off (ends any mirroring session and stops
        advertising). The last frame stays available."""
        await anyio.to_thread.run_sync(receiver.stop)
        return "AirPlay receiver is off."

    @server.tool(annotations=read_only, structured_output=False)
    def screen_status(ctx: Context) -> str:
        """Text-only status (~40 tokens): mirroring connected?, device, resolution,
        current frame number, time since last change, and whether you've already
        seen the current frame. Use this before deciding to fetch an image."""
        return service.status(_client_key(ctx))

    @server.tool(annotations=read_only, structured_output=False)
    async def get_screen(
        ctx: Context,
        mode: Annotated[
            Literal["full", "changes"],
            Field(description="'changes' returns only the area that changed since the frame you last saw"),
        ] = "full",
        max_dim: Annotated[
            int | None,
            Field(ge=128, le=2048, description="Longest image edge in px (default 1024; 512 is enough for a glance)"),
        ] = None,
        region: Annotated[
            list[float] | None,
            Field(
                min_length=4,
                max_length=4,
                description="Zoom to [x, y, w, h] as fractions (0-1) of the screen, e.g. [0, 0.3, 1, 0.5]",
            ),
        ] = None,
        since_frame: Annotated[
            int | None,
            Field(description="Frame number you already have; if still current, returns 'unchanged' text instead of an image"),
        ] = None,
        force: Annotated[bool, Field(description="Send an image even if unchanged")] = False,
        settle_ms: Annotated[
            int, Field(ge=0, le=3000, description="Wait for the screen to be still this long first")
        ] = 300,
        grid: Annotated[
            list[int] | None,
            Field(
                min_length=2,
                max_length=2,
                description="Overlay a labelled [cols, rows] grid (cols A.., rows 1..) to reference cells, "
                "e.g. [9, 9] on a cropped Sudoku board",
            ),
        ] = None,
        format: Annotated[Literal["jpeg", "png"], Field(description="png only for crisp text/pixel art")] = "jpeg",
    ) -> list[str | Image]:
        """The mirrored screen as an image (~700 tokens at defaults for a phone).
        Always returns the current frame (the last one received if the screen is
        static); pass since_frame=N to get 'unchanged' text if it is still frame N. Use mode='changes' after the user acts, or region=[x,y,w,h] to zoom."""
        result = await anyio.to_thread.run_sync(
            lambda: service.get_screen(
                _client_key(ctx),
                mode=mode,
                max_dim=max_dim,
                region=tuple(region) if region else None,  # type: ignore[arg-type]
                since_frame=since_frame,
                force=force,
                settle_ms=settle_ms,
                grid=(grid[0], grid[1]) if grid else None,
                fmt=format,
            )
        )
        if result.image is None:
            return [result.text]
        return [result.text, Image(data=result.image, format=result.mime.split("/")[1])]

    @server.tool(annotations=read_only, structured_output=False)
    async def wait_for_change(
        ctx: Context,
        timeout_s: Annotated[float, Field(ge=1, le=300)] = 30,
        since_frame: Annotated[int | None, Field(description="Frame number you hold (from the get_screen header); waits for a newer one. Defaults to the last frame sent to you")] = None,
    ) -> str:
        """Block until the screen meaningfully changes (status-bar clock ignored),
        then return text only. With no screen yet, waits for the first frame.
        Cheaper than polling get_screen. It does NOT mean 'look at the screen':
        if the user asks you to look and the screen is static, call get_screen."""
        return await anyio.to_thread.run_sync(
            lambda: service.wait_for_change(_client_key(ctx), timeout_s, since_frame), abandon_on_cancel=True
        )

    @server.tool(
        annotations=types.ToolAnnotations(read_only_hint=False, open_world_hint=False),
        structured_output=False,
    )
    async def save_screen(
        path: Annotated[
            str | None,
            Field(description="File to write (.png or .jpg). Relative paths go under the save "
                              "directory; default is a timestamped PNG there"),
        ] = None,
        region: Annotated[
            list[float] | None,
            Field(min_length=4, max_length=4, description="[x, y, w, h] fractions of the screen"),
        ] = None,
        max_dim: Annotated[
            int | None, Field(ge=64, le=8192, description="Downscale so the longest edge is at most this")
        ] = None,
    ) -> str:
        """Save the current screen to an image file at full resolution. No image is
        returned, so this costs almost no tokens. Use it to hand frames to scripts or
        other tools, or to keep a record."""
        if path is None:
            path = f"screen-{datetime.now():%Y%m%d-%H%M%S}-f{store.seq}.png"
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path(args.save_dir).expanduser() / target
        return await anyio.to_thread.run_sync(
            lambda: service.save_screen(str(target), tuple(region) if region else None, max_dim)  # type: ignore[arg-type]
        )

    if ocr.available():

        @server.tool(annotations=read_only, structured_output=False)
        async def read_screen_text(
            region: Annotated[
                list[float] | None,
                Field(min_length=4, max_length=4, description="[x, y, w, h] fractions of the screen"),
            ] = None,
        ) -> str:
            """OCR the screen (or a region) and return text lines with positions:
            usually far fewer tokens than an image for text-heavy screens."""
            got = service.screen_for_ocr(tuple(region) if region else None)  # type: ignore[arg-type]
            if got is None:
                return service.status(None)
            frame, img = got
            lines = await anyio.to_thread.run_sync(ocr.read_text, img)
            body = "\n".join(f"[y={y:.2f} x={x:.2f}] {t}" for x, y, t in lines) or "(no text found)"
            return f"frame {frame.seq} text (positions are fractions of the {'region' if region else 'screen'}):\n{body}"

    @server.resource(SCREEN_URI, name="screen", mime_type="image/jpeg", description="Latest mirrored screen (JPEG)")
    def screen_resource() -> bytes:
        frame = store.latest
        if frame is None:
            return b""
        x0, y0, x1, y1 = imaging.content_box(frame.rgb)
        img = imaging.fit(PILImage.fromarray(frame.rgb[y0:y1, x0:x1]), args.max_dim)
        return imaging.encode(img, "jpeg", args.quality)

    return server


def _client_key(ctx: Context) -> object:
    """Stable per-client identity for dedupe.

    A ServerSession is built per request; the Connection behind it lives as long as the
    client's stdio pipe or HTTP session. Stateless HTTP gets a fresh one per request,
    in which case clients should pass ``since_frame`` explicitly.
    """
    conn = getattr(ctx.session, "_connection", None)  # no public accessor in mcp 2.x
    if conn is None:
        return ctx.session
    return conn.session_id or conn


def _env(name: str, default: object = None) -> object:
    """Option default from AIRPLAY_<NAME>; argparse applies the option's type to string defaults."""
    return os.environ.get(f"AIRPLAY_{name}", default)


def _env_flag(name: str) -> bool:
    return str(_env(name, "")).strip().lower() in ("1", "true", "yes", "on")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        "airplay-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--name", default=_env("NAME", "AI Screen"), help="AirPlay name shown on the phone [AIRPLAY_NAME]")
    p.add_argument("--receiver", choices=["native", "uxplay", "gstreamer", "external"],
                   default=_env("RECEIVER", "native"),
                   help="video source. AirPlay (iPhone can mirror to it): native = built-in pylibairplay "
                        "(default), uxplay = UxPlay program. NOT AirPlay: gstreamer = your own gst-launch "
                        "pipeline, external = raw H.264 on --stream-port [AIRPLAY_RECEIVER]")
    p.add_argument("--pin", default=_env("PIN", "3939"), help="pairing PIN shown on the phone [AIRPLAY_PIN]")
    p.add_argument("--autostart", action="store_true", default=_env_flag("AUTOSTART"),
                   help="turn the receiver on at launch instead of on start_receiver [AIRPLAY_AUTOSTART]")
    p.add_argument("--save-dir", default=_env("SAVE_DIR", str(Path.home() / "airplay-mcp")),
                   help="where save_screen writes relative paths [AIRPLAY_SAVE_DIR]")
    p.add_argument("-v", "--verbose", action="store_true", default=_env_flag("VERBOSE"), help="[AIRPLAY_VERBOSE]")
    p.add_argument("--log-file", default=_env("LOG_FILE"),
                   help="also append logs to this file (MCP clients often discard stderr) [AIRPLAY_LOG_FILE]")

    fp = p.add_argument_group("FairPlay (needed by recent iOS versions)")
    fp.add_argument("--fp-emu-dir", default=_env("FP_EMU_DIR"),
                    help="dir with AirPlayReceiver + sibling dylibs extracted from your own device "
                         "firmware; machine-local, see README [AIRPLAY_FP_EMU_DIR]")
    fp.add_argument("--fp-emu-python", default=_env("FP_EMU_PYTHON"),
                    help="interpreter with unicorn/lief/capstone for the emulator; defaults to this "
                         "one (install the [fairplay] extra) [AIRPLAY_FP_EMU_PYTHON]")

    u = p.add_argument_group("UxPlay (--receiver uxplay)")
    u.add_argument("--uxplay-path", default=_env("UXPLAY_PATH"),
                   help="path to the uxplay executable; default: look on PATH [AIRPLAY_UXPLAY_PATH]")
    u.add_argument("--uxplay-arg", action="append", default=[],
                   help="extra argument for uxplay, repeatable, e.g. --uxplay-arg=-pin")
    u.add_argument("--fps", type=int, default=_env("FPS", 30), help="max fps requested from the phone [AIRPLAY_FPS]")

    g = p.add_argument_group("GStreamer (--receiver gstreamer)")
    g.add_argument("--gst-pipeline", default=_env("GST_PIPELINE"),
                   help="gst-launch-1.0 pipeline producing Annex-B H.264 and ending in "
                        "'tcpclientsink host=127.0.0.1 port={port}'; {port} is filled in [AIRPLAY_GST_PIPELINE]")
    g.add_argument("--gst-path", default=_env("GST_LAUNCH_PATH"),
                   help="path to gst-launch-1.0; default: look on PATH [AIRPLAY_GST_LAUNCH_PATH]")

    t = p.add_argument_group("MCP transport")
    t.add_argument("--transport", choices=["stdio", "http"], default=_env("TRANSPORT", "stdio"),
                   help="[AIRPLAY_TRANSPORT]")
    t.add_argument("--host", default=_env("HOST", "127.0.0.1"),
                   help="HTTP bind address, 0.0.0.0 for LAN [AIRPLAY_HOST]")
    t.add_argument("--port", type=int, default=_env("PORT", 8765),
                   help="HTTP port, endpoint is /mcp [AIRPLAY_PORT]")

    i = p.add_argument_group("images and change detection")
    i.add_argument("--max-dim", type=int, default=_env("MAX_DIM", 1024),
                   help="default longest image edge sent to the model [AIRPLAY_MAX_DIM]")
    i.add_argument("--quality", type=int, default=_env("QUALITY", 80), help="JPEG quality [AIRPLAY_QUALITY]")
    i.add_argument("--ignore-top", type=float, default=_env("IGNORE_TOP", 0.06),
                   help="fraction of a portrait screen's top ignored for change detection [AIRPLAY_IGNORE_TOP]")
    i.add_argument("--min-changed-cells", type=int, default=_env("MIN_CHANGED_CELLS", 2),
                   help="changed cells (~15px each) needed to count as a change [AIRPLAY_MIN_CHANGED_CELLS]")
    i.add_argument("--sample-fps", type=float, default=_env("SAMPLE_FPS", 4.0),
                   help="how often decoded frames are analysed [AIRPLAY_SAMPLE_FPS]")

    d = p.add_argument_group("testing without a phone")
    d.add_argument("--external", action="store_true", default=_env_flag("EXTERNAL"),
                   help="same as --receiver external [AIRPLAY_EXTERNAL]")
    d.add_argument("--stream-port", type=int, default=_env("STREAM_PORT", 0),
                   help="local TCP port for the video stream, 0 = auto [AIRPLAY_STREAM_PORT]")
    d.add_argument("--codec", choices=["h264", "hevc"], default=_env("CODEC", "h264"), help="[AIRPLAY_CODEC]")

    args = p.parse_args(argv)
    if args.external:
        args.receiver = "external"
    for opt in ("transport", "codec", "receiver"):  # argparse doesn't check choices on env-supplied defaults
        action = next(a for a in p._actions if a.dest == opt)
        if getattr(args, opt) not in action.choices:  # type: ignore[operator]
            p.error(f"invalid {opt} {getattr(args, opt)!r} (choose from {', '.join(action.choices)})")  # type: ignore[arg-type]
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # Logs go to stderr: on stdio, stdout is the MCP channel.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if args.log_file:
        handlers.append(logging.FileHandler(Path(args.log_file).expanduser(), encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    server = build_server(args)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
