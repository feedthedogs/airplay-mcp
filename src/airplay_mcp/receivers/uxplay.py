"""Supervise a UxPlay child process (https://github.com/FDH2/UxPlay).

UxPlay builds its video pipeline as ``appsrc ! queue ! h264parse ! rtph264pay
<our -vrtp string>``, so we finish that pipeline by depayloading straight
back to an Annex-B byte stream and sending it over local TCP to the
StreamDecoder. No window is opened and no audio is played.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .base import Receiver, ReceiverStatus

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "UxPlay not found. Install it (macOS: `brew install uxplay`; Debian/Ubuntu: `sudo apt install uxplay`; "
    "Windows: build with MSYS2 per https://github.com/FDH2/UxPlay#building-uxplay-on-microsoft-windows) "
    "and put it on PATH, set UXPLAY_PATH, or pass --uxplay-path."
)

_CLIENT_RE = re.compile(r"connection request from (.+?) \((.+?)\)")


def find_uxplay(explicit: str | None = None) -> str | None:
    for candidate in (explicit, os.environ.get("UXPLAY_PATH")):
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("uxplay")
    if found:
        return found
    if sys.platform == "win32":
        for root in (r"C:\msys64\ucrt64\bin", r"C:\msys64\clangarm64\bin", r"C:\msys64\mingw64\bin"):
            exe = Path(root) / "uxplay.exe"
            if exe.is_file():
                return str(exe)
    return None


def vrtp_pipeline(port: int) -> str:
    # UxPlay rewrites "h264" <-> "h265" in this string to match the stream it gets.
    return (
        "config-interval=-1 ! rtph264depay ! h264parse config-interval=-1 ! "
        "video/x-h264,stream-format=byte-stream,alignment=au ! "
        f"tcpclientsink host=127.0.0.1 port={port} sync=false"
    )


class UxPlayReceiver(Receiver):
    def __init__(
        self,
        stream_port: int,
        name: str = "AI Screen",
        uxplay_path: str | None = None,
        fps: int = 30,
        extra_args: list[str] | None = None,
    ) -> None:
        self.name = name
        self.stream_port = stream_port
        self.fps = fps
        self.extra_args = extra_args or []
        self.binary = find_uxplay(uxplay_path)
        self.client: str | None = None
        self._log: collections.deque[str] = collections.deque(maxlen=40)
        self._proc: subprocess.Popen[str] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._detail = ""

    def command(self) -> list[str]:
        assert self.binary
        return [
            self.binary,
            "-n", self.name,
            "-nh",  # don't append "@hostname" to the advertised name
            "-as", "0",  # no audio
            "-fps", str(self.fps),
            "-vrtp", vrtp_pipeline(self.stream_port),
            *self.extra_args,
        ]  # fmt: skip

    def start(self) -> None:
        if not self.binary:
            self._detail = INSTALL_HINT
            log.error(INSTALL_HINT)
            return
        self._thread = threading.Thread(target=self._supervise, name="uxplay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def status(self) -> ReceiverStatus:
        running = self._proc is not None and self._proc.poll() is None
        return ReceiverStatus(running, self.name, self._detail, self.client, list(self._log)[-8:])

    def _supervise(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            started = time.monotonic()
            cmd = self.command()
            log.info("starting %s", " ".join(cmd))
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            try:
                # stdout must be captured: on the stdio transport our own stdout is the MCP channel.
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=flags,
                )
            except OSError as e:
                self._detail = f"failed to launch uxplay: {e}"
                log.error(self._detail)
                return
            self._detail = "advertising; waiting for a device to start Screen Mirroring"
            assert self._proc.stdout
            for line in self._proc.stdout:
                self._on_line(line.rstrip())
            code = self._proc.wait()
            if self._stop.is_set():
                break
            self._detail = f"uxplay exited with code {code}; restarting"
            log.warning(self._detail)
            backoff = 1.0 if time.monotonic() - started > 30 else min(backoff * 2, 30)
            self._stop.wait(backoff)

    def _on_line(self, line: str) -> None:
        if not line:
            return
        self._log.append(line)
        log.debug("uxplay: %s", line)
        if m := _CLIENT_RE.search(line):
            self.client = f"{m.group(1)} ({m.group(2)})"
