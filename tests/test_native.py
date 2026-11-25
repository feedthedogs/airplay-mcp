"""NativeReceiver hands pylibairplay access units straight to the decoder sink."""
import types

from airplay_mcp.receivers import NativeReceiver


class Sink:
    def __init__(self) -> None:
        self.events: list[tuple[str, bytes | None]] = []

    def begin_stream(self) -> None:
        self.events.append(("begin", None))

    def submit_au(self, data: bytes) -> None:
        self.events.append(("au", data))

    def end_stream(self) -> None:
        self.events.append(("end", None))


SESSION = types.SimpleNamespace(device_name="Test iPhone", model="iPhone17,3", width=498, height=1080)


def test_session_aus_and_end_reach_the_sink_in_order():
    sink = Sink()
    rx = NativeReceiver(sink, name="AI Screen")
    au1 = types.SimpleNamespace(data=b"\x00\x00\x00\x01\x67abc", keyframe=True)
    au2 = types.SimpleNamespace(data=b"\x00\x00\x00\x01\x41def", keyframe=False)
    rx._on_session(SESSION)
    rx._on_video(au1)
    rx._on_video(au2)
    rx._on_end(types.SimpleNamespace(value="client_stopped"))
    rx._on_end(types.SimpleNamespace(value="client_stopped"))  # the library reports the end twice
    assert sink.events == [("begin", None), ("au", au1.data), ("au", au2.data), ("end", None)]


def test_video_without_session_still_starts_a_stream():
    sink = Sink()
    rx = NativeReceiver(sink)
    rx._on_video(types.SimpleNamespace(data=b"x"))
    assert sink.events == [("begin", None), ("au", b"x")]


def test_session_and_end_update_status():
    rx = NativeReceiver(Sink(), name="AI Screen")
    rx._on_session(SESSION)
    st = rx.status()
    assert rx.client == "Test iPhone (iPhone17,3)" and st.detail == "mirroring"
    rx._on_end(types.SimpleNamespace(value="client_stopped"))
    assert "stopped" in rx.status().detail
