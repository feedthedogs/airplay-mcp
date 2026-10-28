from airplay_mcp.receivers.uxplay import UxPlayReceiver, vrtp_pipeline


def test_command_forwards_stream_to_decoder_port(tmp_path) -> None:
    fake = tmp_path / "uxplay"
    fake.write_text("")
    rx = UxPlayReceiver(5600, name="AI Screen", uxplay_path=str(fake), extra_args=["-pin"])
    cmd = rx.command()
    assert cmd[0] == str(fake)
    assert cmd[cmd.index("-n") + 1] == "AI Screen"
    assert "-nh" in cmd and cmd[cmd.index("-as") + 1] == "0"
    assert cmd[cmd.index("-vrtp") + 1] == vrtp_pipeline(5600)
    assert cmd[-1] == "-pin"


def test_vrtp_pipeline_continues_after_rtph264pay() -> None:
    # UxPlay prepends "rtph264pay ", so the string must start with its properties.
    pl = vrtp_pipeline(5600)
    assert pl.startswith("config-interval=")
    assert "rtph264depay ! h264parse" in pl and pl.endswith("port=5600 sync=false")


def test_missing_binary_reports_install_hint(monkeypatch) -> None:
    monkeypatch.delenv("UXPLAY_PATH", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("sys.platform", "linux")
    rx = UxPlayReceiver(5600)
    rx.start()
    st = rx.status()
    assert not st.running and "brew install uxplay" in st.detail


def test_client_name_parsed_from_log() -> None:
    rx = UxPlayReceiver(5600, uxplay_path=None)
    rx._on_line("connection request from My iPhone (iPhone15,2) with deviceID = AA:BB")
    assert rx.client == "My iPhone (iPhone15,2)"
