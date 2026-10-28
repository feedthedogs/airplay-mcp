"""Native AirPlay receiver built on the `pylibairplay` library.

Runs pylibairplay's async receiver on a background thread. pylibairplay handles
discovery, pairing and (for a modern iPhone) FairPlay — decrypting the screen
stream via a local firmware-whitebox emulator — and hands us decoded-ready
Annex-B H.264 access units. We forward those bytes to the StreamDecoder's TCP
port, so the rest of the server just sees an H.264 stream.

FairPlay needs the user's own extracted firmware dylibs present at runtime
(``fp_emu_dir``) and a Python with unicorn/lief/capstone (``fp_emu_python``);
it is machine-local interop tooling, not something shipped to other machines.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import socket
import threading

from .base import Receiver, ReceiverStatus

log = logging.getLogger(__name__)


class NativeReceiver(Receiver):
    def __init__(
        self,
        stream_port: int,
        name: str = "AI Screen",
        fp_emu_dir: str | None = None,
        fp_emu_python: str | None = None,
        pin: str = "3939",
    ) -> None:
        self.stream_port = stream_port
        self.name = name
        self.fp_emu_dir = fp_emu_dir
        self.fp_emu_python = fp_emu_python
        self.pin = pin
        self.client: str | None = None
        self._detail = ""
        self._log: collections.deque[str] = collections.deque(maxlen=40)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._rx = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    # ------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.client = None
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="pylibairplay", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            from pylibairplay.receiver import Receiver as PyReceiver
        except Exception as e:  # pragma: no cover - import/env issue
            self._detail = f"pylibairplay not importable: {e}"
            log.error(self._detail)
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        rx = PyReceiver(
            name=self.name,
            pin=self.pin,
            legacy=bool(self.fp_emu_dir),
            fp_emu_dir=self.fp_emu_dir,
            fp_emu_python=self.fp_emu_python,
        )
        rx.on_video = self._on_video
        rx.on_session = self._on_session
        rx.on_end = self._on_end
        self._rx = rx
        self._detail = "advertising; waiting for a device to start Screen Mirroring"
        if self.fp_emu_dir:
            self._detail += " (warming FairPlay emulator, ~35s)"
        if self._stopping.is_set():  # stop() raced with startup
            loop.close()
            return
        self._task = loop.create_task(rx.serve())
        try:
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            self._detail = "stopped"
        except Exception as e:  # pragma: no cover
            self._detail = f"pylibairplay error: {e}"
            log.exception("pylibairplay receiver crashed")
        finally:
            self._close_sock()
            pending = asyncio.all_tasks(loop)  # e.g. zeroconf's broadcast tasks
            for t in pending:
                t.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._loop = self._task = self._rx = None

    def stop(self) -> None:
        self._stopping.set()
        self._close_sock()
        loop, thread = self._loop, self._thread
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=5)
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=5)
        self.client = None
        self._detail = "stopped"

    async def _shutdown(self) -> None:
        # Runs on the receiver loop, so _rx/_task are read after startup has set them.
        if self._rx is not None:
            await self._rx.close()
        if self._task is not None:
            self._task.cancel()

    def status(self) -> ReceiverStatus:
        return ReceiverStatus(self.running, self.name, self._detail, self.client, list(self._log)[-8:])

    # --------------------------------------------------------------- streaming
    def _on_video(self, au) -> None:
        """Forward an Annex-B access unit to the StreamDecoder (connect lazily)."""
        try:
            sock = self._ensure_sock()
            sock.sendall(au.data)
        except OSError as e:
            log.warning("stream write failed: %s", e)
            self._close_sock()

    def _ensure_sock(self) -> socket.socket:
        with self._lock:
            if self._sock is None:
                s = socket.create_connection(("127.0.0.1", self.stream_port))
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._sock = s
                log.info("forwarding H.264 to decoder on 127.0.0.1:%d", self.stream_port)
            return self._sock

    def _close_sock(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    def _on_session(self, info) -> None:
        self.client = f"{info.device_name} ({info.model})" if info.model else info.device_name
        self._detail = "mirroring"
        self._log.append(f"session: {self.client} {info.width}x{info.height}")
        log.info("screen session: %s %dx%d", self.client, info.width, info.height)

    def _on_end(self, reason) -> None:
        # Close the decoder connection so it sees EOF and resets `streaming`; the next
        # mirroring session reconnects with a fresh keyframe.
        self._close_sock()
        self._detail = "advertising; mirroring stopped"
        self._log.append(f"ended: {getattr(reason, 'value', reason)}")
