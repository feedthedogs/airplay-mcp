# Privacy policy

airplay-mcp (also distributed as the "AirPlay Screen" Claude plugin and the `airplay-screen` ClawHub skill) runs entirely on your own computer. It has no servers, accounts, analytics or telemetry.

## What it handles

- **Your phone's screen.** While you mirror to the receiver, video frames are decoded and held in memory on your computer. A frame reaches your AI client only when the agent calls a tool such as `get_screen`. The client then handles it under that client's own privacy terms (for Claude, Anthropic's).
- **Saved frames.** `save_screen` writes image files only where the agent asks, and relative paths go under `AIRPLAY_SAVE_DIR` (default `~/airplay-mcp`). Nothing else is written to disk, except an optional log file if you set `AIRPLAY_LOG_FILE`.
- **FairPlay files.** If you use the built-in receiver, the files you extract from your own iPhone firmware stay in the folder you choose. airplay-mcp only reads them and never copies or uploads them.
- **Network.** The receiver listens on your local network so your phone can connect, and advertises itself over mDNS. It makes no outbound connections. The setup instructions download software (uv, ipsw, UxPlay, Python packages) and Apple firmware from their usual public sources, and only when you agree.

## Retention

Frames in memory are discarded when the receiver stops or the server exits. Files you saved stay until you delete them.

## Children

airplay-mcp is not directed at children under 18.

## Contact

Report issues or questions at https://github.com/feedthedogs/airplay-mcp/issues.
