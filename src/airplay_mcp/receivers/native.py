"""Native AirPlay receiver built on the `pylibairplay` library.

Runs pylibairplay's async receiver on a background thread. pylibairplay handles
discovery, pairing and FairPlay (pure Python), and hands us decoded-ready
Annex-B H.264 access units. Each one goes straight onto the StreamDecoder's
queue as a whole packet: no blocking I/O on the receiver's event loop, and no
re-parsing of a byte stream (which would hold back the newest frame).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
from typing import Protocol

from .base import Receiver, ReceiverStatus

log = logging.getLogger(__name__)


class VideoSink(Protocol):
    """Where access units go (StreamDecoder). All methods must be thread-safe and non-blocking."""

    def begin_stream(self) -> None: ...
    def submit_au(self, data: bytes) -> None: ...
    def end_stream(self) -> None: ...


class NativeReceiver(Receiver):
    def __init__(
        self,
        sink: VideoSink,
        name: str = "AI Screen",
        pin: str = "3939",
    ) -> None:
        self.sink = sink
        self._in_stream = False
        self.name = name
        self.pin = pin
        self.client: str | None = None
        self._detail = ""
        self._log: collections.deque[str] = collections.deque(maxlen=40)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._rx = None
        self._thread: threading.Thread | None = None
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
            legacy=True,
        )
        rx.on_video = self._on_video
        rx.on_session = self._on_session
        rx.on_end = self._on_end
        self._rx = rx
        self._detail = "advertising; waiting for a device to start Screen Mirroring"
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
            self._end_stream()
            pending = asyncio.all_tasks(loop)  # e.g. zeroconf's broadcast tasks
            for t in pending:
                t.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            self._loop = self._task = self._rx = None

    def stop(self) -> None:
        self._stopping.set()
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
        if self._rx is not None:
            await self._rx.close()
        if self._task is not None:
            self._task.cancel()

    def status(self) -> ReceiverStatus:
        return ReceiverStatus(self.running, self.name, self._detail, self.client, list(self._log)[-8:])

    # --------------------------------------------------------------- streaming
    def _on_video(self, au) -> None:
        """Hand an Annex-B access unit to the decoder (runs on the receiver's event loop)."""
        if not self._in_stream:  # e.g. video without a session callback
            self._begin_stream()
        self.sink.submit_au(au.data)

    def _begin_stream(self) -> None:
        self.sink.begin_stream()
        self._in_stream = True

    def _end_stream(self) -> None:
        if self._in_stream:
            self._in_stream = False
            self.sink.end_stream()

    def _on_session(self, info) -> None:
        self.client = f"{info.device_name} ({info.model})" if info.model else info.device_name
        self._detail = "mirroring"
        self._begin_stream()  # a new session gets a fresh decoder
        self._log.append(f"session: {self.client} {info.width}x{info.height}")
        log.info("screen session: %s %dx%d", self.client, info.width, info.height)

    def _on_end(self, reason) -> None:
        self._end_stream()
        self._detail = "advertising; mirroring stopped"
        self._log.append(f"ended: {getattr(reason, 'value', reason)}")
