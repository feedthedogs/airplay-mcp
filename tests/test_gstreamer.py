from airplay_mcp.receivers.gstreamer import GStreamerReceiver


def test_command_fills_in_port_and_splits_pipeline(tmp_path) -> None:
    fake = tmp_path / "gst-launch-1.0"
    fake.write_text("")
    rx = GStreamerReceiver(5600, "videotestsrc ! tcpclientsink host=127.0.0.1 port={port}", str(fake))
    cmd = rx.command()
    assert cmd[:2] == [str(fake), "-q"]
    assert cmd[-1] == "port=5600" and "videotestsrc" in cmd


def test_missing_pipeline_is_reported() -> None:
    rx = GStreamerReceiver(5600, None)
    rx.start()
    st = rx.status()
    assert not st.running and "--gst-pipeline" in st.detail


def test_missing_binary_reports_install_hint(monkeypatch) -> None:
    monkeypatch.delenv("GST_LAUNCH_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)
    rx = GStreamerReceiver(5600, "videotestsrc ! fakesink")
    rx.start()
    st = rx.status()
    assert not st.running and "brew install gstreamer" in st.detail
