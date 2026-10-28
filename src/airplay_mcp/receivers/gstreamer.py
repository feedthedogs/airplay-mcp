"""Run a user-supplied GStreamer pipeline as the video source.

For anything GStreamer can read that isn't AirPlay (an RTSP/UDP feed from a
scrcpy or Miracast bridge, a capture card, a test clip, ...). The pipeline must
encode to Annex-B H.264 and end by sending it to this server, so it contains
``{port}`` where the decoder's local TCP port goes, e.g.::

    videotestsrc is-live=true ! x264enc tune=zerolatency ! h264parse config-interval=-1 !
      video/x-h264,stream-format=byte-stream,alignment=au ! tcpclientsink host=127.0.0.1 port={port}
"""

from __future__ import annotations

import collections
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading

from .base import Receiver, ReceiverStatus

log = logging.getLogger(__name__)

INSTALL_HINT = (
    "GStreamer not found. Install it (macOS: `brew install gstreamer`; Debian/Ubuntu: "
    "`sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad "
    "gstreamer1.0-plugins-ugly gstreamer1.0-libav`; Windows: the MSVC runtime installer "
    "from https://gstreamer.freedesktop.org/download/) and put `gst-launch-1.0` on PATH, "
    "or pass --gst-path."
)


def find_gst_launch(explicit: str | None = None) -> str | None:
    for candidate in (explicit, os.environ.get("GST_LAUNCH_PATH")):
        if candidate and os.path.isfile(candidate):
            return candidate
    return shutil.which("gst-launch-1.0")


class GStreamerReceiver(Receiver):
    def __init__(self, stream_port: int, pipeline: str | None, gst_path: str | None = None) -> None:
        self.stream_port = stream_port
        self.pipeline = pipeline
        self.binary = find_gst_launch(gst_path)
        self._log: collections.deque[str] = collections.deque(maxlen=40)
        self._proc: subprocess.Popen[str] | None = None
        self._detail = ""

    def command(self) -> list[str]:
        assert self.binary and self.pipeline
        return [self.binary, "-q", *shlex.split(self.pipeline.replace("{port}", str(self.stream_port)))]

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        if not self.pipeline:
            self._detail = "no GStreamer pipeline: pass --gst-pipeline (see README)"
        elif not self.binary:
            self._detail = INSTALL_HINT
        else:
            cmd = self.command()
            log.info("starting %s", " ".join(cmd))
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            try:
                # stdout is captured: on the stdio transport our own stdout is the MCP channel.
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
                )
            except OSError as e:
                self._detail = f"failed to launch gst-launch-1.0: {e}"
            else:
                self._detail = "gst-launch-1.0 running; waiting for video"
                threading.Thread(target=self._pump, args=(self._proc,), name="gstreamer", daemon=True).start()
                return
        log.error(self._detail)

    def stop(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            self._detail = "stopped"
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def status(self) -> ReceiverStatus:
        return ReceiverStatus(self.running, "GStreamer pipeline", self._detail, None, list(self._log)[-8:])

    def _pump(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout
        for line in proc.stdout:
            if line := line.rstrip():
                self._log.append(line)
                log.debug("gst: %s", line)
        code = proc.wait()
        if self._proc is proc and self._detail != "stopped":
            self._detail = f"gst-launch-1.0 exited with code {code}"
            log.warning(self._detail)
