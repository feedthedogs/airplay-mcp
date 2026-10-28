"""NativeReceiver forwards pylibairplay access units to the decoder TCP port."""
import socket
import types

from airplay_mcp.receivers import NativeReceiver


def _listener():
    s = socket.create_server(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_on_video_forwards_annexb_to_port():
    srv, port = _listener()
    try:
        rx = NativeReceiver(port, name="AI Screen")
        au1 = types.SimpleNamespace(data=b"\x00\x00\x00\x01\x67abc", keyframe=True)
        au2 = types.SimpleNamespace(data=b"\x00\x00\x00\x01\x41def", keyframe=False)
        rx._on_video(au1)
        conn, _ = srv.accept()
        rx._on_video(au2)
        conn.settimeout(2)
        got = b""
        while len(got) < len(au1.data) + len(au2.data):
            chunk = conn.recv(4096)
            if not chunk:
                break
            got += chunk
        assert got == au1.data + au2.data
        conn.close()
    finally:
        rx._close_sock()
        srv.close()


def test_session_and_end_update_status():
    srv, port = _listener()
    try:
        rx = NativeReceiver(port, name="AI Screen")
        rx._on_session(types.SimpleNamespace(device_name="Test iPhone", model="iPhone17,3",
                                             width=498, height=1080))
        st = rx.status()
        assert rx.client == "Test iPhone (iPhone17,3)" and st.detail == "mirroring"
        rx._on_end(types.SimpleNamespace(value="client_stopped"))
        assert "stopped" in rx.status().detail
    finally:
        rx._close_sock()
        srv.close()
