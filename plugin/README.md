# AirPlay Screen

Lets Claude see your iPhone or iPad. The plugin runs [airplay-mcp](https://github.com/feedthedogs/airplay-mcp), a local AirPlay receiver. You mirror your phone to it from Control Centre, and Claude can then look at the screen, wait for it to change, or save frames to disk.

It runs on your own computer. Claude Code (or another client that runs local MCP servers) must be on the **same Wi-Fi** as the phone. It does not work from claude.ai in a browser.

## Install

```
/plugin install airplay-mcp --marketplace feedthedogs/airplay-mcp
```

Then run `/airplay-mcp:setup`. The agent installs [uv](https://docs.astral.sh/uv/) and asks which receiver you want:

- **native**: Recent iOS needs Apple's FairPlay check. The agent extracts the needed files from **your own** iPhone firmware with [ipsw](https://github.com/blacktop/ipsw); they never leave your machine.
- **uxplay**: The agent installs [UxPlay](https://github.com/FDH2/UxPlay), which handles FairPlay itself.

The agent then tells you which settings to fill in under `/plugin`.

## Example prompts

- *"Turn on the AirPlay screen, wait for my phone, and tell me the next move in this Sudoku."*
- *"Look at the crossword on my phone and solve the highlighted clue."*
- *"Start AirPlay and save a screenshot to ./shots every time my screen changes, until I say stop."*
- *"I'm going to scroll through a long article on my phone. Read along and summarise it when I'm done."*

## Testing without a phone

Set the **Receiver** option to `external`. The server then accepts a raw H.264 stream on a local port instead of AirPlay. `start_receiver` reports the port; send a clip with `ffmpeg -re -i clip.mp4 -an -c:v libx264 -f h264 tcp://127.0.0.1:<port>`.

## Privacy and support

Frames stay in memory on your computer and reach Claude only when the agent asks for them. airplay-mcp has no servers or telemetry. See the [privacy policy](https://github.com/feedthedogs/airplay-mcp/blob/main/PRIVACY.md). Report issues at https://github.com/feedthedogs/airplay-mcp/issues. The server is MIT-licensed; its full documentation is in the [main README](https://github.com/feedthedogs/airplay-mcp#readme).
