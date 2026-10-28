# airplay-mcp

An MCP server that turns your computer into an **AirPlay receiver** an AI agent can see through. The agent switches the receiver on, you mirror your iPhone or iPad to it, and the agent gets the phone screen frame by frame. It can look at the screen, wait for it to change, or save frames to disk for whatever processing it has been asked to do.

Works with any MCP client (Claude Code, Claude Desktop, Cursor, …). No external apps, no GStreamer: the AirPlay protocol is handled in-process by the `pylibairplay` library. UxPlay (an AirPlay receiver) and a GStreamer pipeline source (not AirPlay) are supported as opt-in alternatives ([see below](#alternative-receivers-uxplay-gstreamer-pipeline)).

## Getting started

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), an iPhone or iPad, and a computer on the same Wi-Fi/LAN as the phone. Allow the firewall prompt on first start: the phone has to reach the receiver on TCP 7000 and mDNS (UDP 5353).

Modern iOS refuses to send the screen stream until the receiver passes Apple's FairPlay check. FairPlay can't be reimplemented from scratch, so steps 3-4 pull the needed binaries out of **your own device's firmware** and step 5 points airplay-mcp at them. Those files are Apple's and stay on your machine. Skip them and the phone will find the receiver and pair, but no video will arrive.

### 1. Clone the repo

```sh
git clone https://github.com/feedthedogs/airplay-mcp airplaymcp
cd airplaymcp
```

The AirPlay protocol library, [`pylibairplay`](https://github.com/feedthedogs/pylibairplay), is fetched automatically by step 2. It includes the FairPlay emulator, so there is nothing else to clone.

### 2. Create the virtual environment and install dependencies

```sh
uv sync --extra fairplay --extra ocr
```

`uv sync` creates `.venv/` and installs everything pinned in `uv.lock`. The extras are optional:

| Extra | Adds |
|---|---|
| `fairplay` | `unicorn`, `lief`, `capstone`, so the FairPlay emulator runs in this same environment. Needed for real iPhones |
| `ocr` | the `read_screen_text` tool (RapidOCR) |

Check it works: `uv run pytest` and `uv run airplay-mcp --help`.

### 3. Install `ipsw`

[`ipsw`](https://github.com/blacktop/ipsw) downloads and unpacks Apple firmware. On macOS: `brew install blacktop/tap/ipsw`. On Windows or Linux, download a build from its [releases page](https://github.com/blacktop/ipsw/releases) and put it on your `PATH`.

### 4. Extract the FairPlay dylibs from your firmware

Use the model identifier of the phone you will mirror from (for example `iPhone17,3`). Settings > General > About shows the model number, and `ipsw device-list` maps it to the identifier. The firmware download is several GB.

```sh
ipsw download ipsw --device iPhone17,3 --latest                  # 1. fetch the IPSW
ipsw extract --dyld --dyld-arch arm64e iPhone17,3_*.ipsw -o fw   # 2. pull the dyld shared cache
ipsw dyld extract fw/*/dyld_shared_cache_arm64e -o fpdir AirPlayReceiver AirPlaySupport CoreUtils AirPlaySender   # 3. extract the four dylibs
```

If the glob in step 3 matches nothing, find the cache with `ls fw/*`. `fpdir/` should now hold the four files. Don't commit or share it.

### 5. Run it and point it at the dylibs

```sh
uv run airplay-mcp --fp-emu-dir "$PWD/fpdir" --autostart -v
```

(Use the absolute path to `fpdir`; in PowerShell write `"$PWD\fpdir"`.)

(`AIRPLAY_FP_EMU_DIR` does the same as `--fp-emu-dir`.) The emulator takes about 35 seconds to warm up.

### 6. Connect the phone

On the iPhone: **Control Centre > Screen Mirroring > "AI Screen"** (enter `3939` if it asks for a code). The server's log shows when the first frame arrives.

### 7. Add it to your MCP client

See below. Once that is done you normally don't run `airplay-mcp` by hand: the client starts it.

### Troubleshooting

- **The phone doesn't list "AI Screen"**: same network? VPNs and guest Wi-Fi often block mDNS. Check the firewall allowed Python.
- **It pairs but the screen stays blank**: FairPlay isn't active. Check `--fp-emu-dir` has all four dylibs and `unicorn`/`lief`/`capstone` are installed (`--extra fairplay`), and wait out the warm-up.
- **Test without a phone**: see [Testing without a phone](#testing-without-a-phone).

## Add it to your MCP client

Run these from inside the `airplaymcp` folder so the paths fill themselves in. They use `uv run --directory`, which needs no global install.

**Claude Code** (macOS/Linux/Git Bash)
```sh
claude mcp add airplay -e AIRPLAY_FP_EMU_DIR="$PWD/fpdir" -- uv run --directory "$PWD" airplay-mcp
```

**Claude Code** (PowerShell)
```powershell
claude mcp add airplay -e AIRPLAY_FP_EMU_DIR="$PWD\fpdir" -- uv run --directory "$PWD" airplay-mcp
```

The receiver then starts when the agent calls `start_receiver`. Restart the client afterwards if it was already open.

**Claude Desktop, Cursor, and other JSON configs**
```json
{
  "mcpServers": {
    "airplay": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/airplaymcp", "airplay-mcp"],
      "env": {
        "AIRPLAY_NAME": "AI Screen",
        "AIRPLAY_SAVE_DIR": "/path/to/where/screenshots/go",
        "AIRPLAY_FP_EMU_DIR": "/path/to/airplaymcp/fpdir"
      }
    }
  }
}
```

Replace the three `/path/to/...` values with real absolute paths (on Windows use forward slashes or doubled backslashes). `AIRPLAY_SAVE_DIR` is optional and defaults to `~/airplay-mcp`.

### Configuration

Each setting can be passed as an environment variable (as above) or a command-line flag. `airplay-mcp --help` lists everything.

| Env var | Flag | Default | Meaning |
|---|---|---|---|
| `AIRPLAY_NAME` | `--name` | `AI Screen` | Name the phone shows under Screen Mirroring |
| `AIRPLAY_PIN` | `--pin` | `3939` | Pairing code, if the phone asks for one |
| `AIRPLAY_SAVE_DIR` | `--save-dir` | `~/airplay-mcp` | Where `save_screen` writes files given as relative paths |
| `AIRPLAY_AUTOSTART` | `--autostart` | off | Start the receiver at launch, not when the agent calls `start_receiver` |
| `AIRPLAY_FP_EMU_DIR` | `--fp-emu-dir` | — | Enables FairPlay (see below) |
| `AIRPLAY_FP_EMU_PYTHON` | `--fp-emu-python` | this Python | Interpreter with `unicorn`/`lief`/`capstone`, if not using the `fairplay` extra |
| `AIRPLAY_MAX_DIM` | `--max-dim` | `1024` | Default longest edge of images sent to the model |
| `AIRPLAY_TRANSPORT` | `--transport` | `stdio` | `http` serves streamable HTTP at `http://HOST:PORT/mcp` |
| `AIRPLAY_HOST` / `AIRPLAY_PORT` | `--host` / `--port` | `127.0.0.1` / `8765` | HTTP bind address (no auth, so only expose it on trusted networks) |

The tuning options (`--ignore-top`, `--min-changed-cells`, `--sample-fps`, `--quality`) rarely need changing.

### FairPlay

Setup is in [Getting started](#getting-started). In short: iOS requires FairPlay, so pylibairplay runs the FairPlay code from **your own device firmware** under a local emulator. The firmware files are never shipped. `AIRPLAY_FP_EMU_DIR` must point at them, and the emulator takes about 35 seconds to warm up after `start_receiver`.

## Alternative receivers (UxPlay, GStreamer pipeline)

The built-in pylibairplay receiver is the default and needs nothing else installed. Two alternatives are available, but only if you ask for them with `--receiver` (or `AIRPLAY_RECEIVER`). They are not used otherwise, and you can ignore this section if the steps above work for you.

> **Only `native` and `uxplay` can be mirrored to from an iPhone.** GStreamer cannot receive AirPlay, so `--receiver gstreamer` never shows up in Screen Mirroring. It is for non-AirPlay video sources.

| `--receiver` | Appears in iPhone Screen Mirroring? | What it is | Needs |
|---|---|---|---|
| `native` (default) | Yes | pylibairplay, in-process | Your own firmware's FairPlay files (see [Getting started](#getting-started)) |
| `uxplay` | Yes | [UxPlay](https://github.com/FDH2/UxPlay), a separate AirPlay receiver program that is itself built on GStreamer | UxPlay installed |
| `gstreamer` | **No** | A `gst-launch-1.0` pipeline you write, as a video source for something that is not AirPlay | GStreamer installed, plus a pipeline |
| `external` | **No** | Accepts raw H.264 pushed to a local port (for testing) | Nothing |

If your goal is "mirror my iPhone and I'd rather not extract firmware", use `uxplay` (it uses GStreamer internally, and you install GStreamer as part of its build). Do not use `gstreamer`.

### UxPlay

[UxPlay](https://github.com/FDH2/UxPlay) (GPL-3, built on GStreamer) is a separate AirPlay receiver program that handles FairPlay itself, so no firmware extraction is needed. airplay-mcp runs it as a child process and takes the video from it.

1. Install UxPlay 1.73 or later so `uxplay` is on your `PATH`:
   - macOS: `brew install uxplay`
   - Debian/Ubuntu: `sudo apt install uxplay` (if your distro's version is older than 1.73, build from source)
   - Windows: build it with MSYS2 (tested with UxPlay 1.74). Install MSYS2 with `winget install MSYS2.MSYS2`, open the **MSYS2 UCRT64** app from the Start menu, and paste:
     ```sh
     pacman -Syu --noconfirm   # may close the window; reopen MSYS2 UCRT64 and run it again
     pacman -S --noconfirm --needed git mingw-w64-ucrt-x86_64-{toolchain,cmake,ninja,libplist,openssl,gstreamer,gst-plugins-base,gst-plugins-good,gst-plugins-bad,gst-plugins-ugly,gst-libav}
     git clone --depth 1 https://github.com/FDH2/UxPlay.git ~/UxPlay && cd ~/UxPlay
     cmake -S . -B build -G Ninja && cmake --build build
     ```
     Then add `C:\msys64\ucrt64\bin` to your Windows `PATH` (uxplay needs the DLLs in it), set `AIRPLAY_UXPLAY_PATH` to `C:\msys64\home\<your user>\UxPlay\build\uxplay.exe`, allow `uxplay.exe` through the firewall, and set your Wi-Fi profile to **Private**.
2. Add it to your client, from inside the `airplaymcp` folder:

```sh
claude mcp add airplay -e AIRPLAY_RECEIVER=uxplay -- uv run --directory "$PWD" airplay-mcp
```

In a JSON config, use `"env": { "AIRPLAY_RECEIVER": "uxplay" }`. If `uxplay` isn't on `PATH`, add `AIRPLAY_UXPLAY_PATH` with the full path to it. Extra UxPlay flags go in `--uxplay-arg` (repeatable, written `--uxplay-arg=-pin`).

### GStreamer pipeline source (not AirPlay)

**This does not make an AirPlay target and your phone will not see it.** To mirror an iPhone, use `native` or `uxplay` above.

`--receiver gstreamer` runs a `gst-launch-1.0` pipeline that you supply and reads H.264 video from it. GStreamer can't receive AirPlay by itself, so this is for other video sources: an RTSP/UDP feed from a screen-capture bridge (scrcpy, Miracast), a capture card, or a test pattern. The pipeline must output Annex-B H.264 and end with `tcpclientsink host=127.0.0.1 port={port}`; airplay-mcp fills in `{port}`.

1. Install GStreamer so `gst-launch-1.0` is on your `PATH`:
   - macOS: `brew install gstreamer`
   - Debian/Ubuntu: `sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav`
   - Windows: the easiest route is MSYS2 (the same install as the UxPlay steps above). In **MSYS2 UCRT64** run `pacman -S --noconfirm --needed mingw-w64-ucrt-x86_64-{gstreamer,gst-plugins-base,gst-plugins-good,gst-plugins-bad,gst-plugins-ugly,gst-libav}`, then add `C:\msys64\ucrt64\bin` to your Windows `PATH`. (The installer from [gstreamer.freedesktop.org/download](https://gstreamer.freedesktop.org/download/) also works; its `winget` package can fail silently if the admin prompt is declined.)
2. Try it with a test pattern (no phone involved; you will see colour bars, not your phone):

```sh
claude mcp add airplay-gst -e AIRPLAY_RECEIVER=gstreamer \
  -e AIRPLAY_GST_PIPELINE="videotestsrc is-live=true pattern=smpte ! video/x-raw,width=640,height=360 ! x264enc tune=zerolatency ! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au ! tcpclientsink host=127.0.0.1 port={port}" \
  -- uv run --directory "$PWD" airplay-mcp
```

Then ask the agent to call `start_receiver` and `get_screen`. Swap `videotestsrc ... x264enc` for your real source, for example `rtspsrc location=rtsp://... ! rtph264depay` if the feed is already H.264.

## How an agent uses it

The server explains this flow to the model in its MCP instructions. A typical session:

1. **`start_receiver`**: the receiver starts advertising. The reply tells the agent what to say to the user: *"Open Control Centre → Screen Mirroring and pick 'AI Screen'"*.
2. **`wait_for_change`**: blocks until the first frame arrives, and afterwards until the screen next changes. Returns text only.
3. **`get_screen`** to look at the screen, or **`save_screen`** to write a full-resolution PNG/JPEG to disk for scripts and other tools. `save_screen` returns only the file path, so it costs almost no tokens.
4. Repeat steps 2 and 3 as the user works on the phone.
5. **`stop_receiver`** when finished.

Example prompts: *"Turn on the AirPlay screen, wait for my phone, and tell me the next move in this Sudoku."* *"Start AirPlay and save a screenshot to ./shots every time my screen changes, until I say stop."*

### Tools

| Tool | What it does |
|---|---|
| `start_receiver()` / `stop_receiver()` | Switch the AirPlay receiver on or off. Both are safe to repeat |
| `screen_status()` | Text only: receiver on/off, device connected, resolution, frame number, time since the last change |
| `wait_for_change(timeout_s, since_frame)` | Block until the first frame or the next real change (the status-bar clock is ignored) |
| `get_screen(mode, max_dim, region, since_frame, force, settle_ms, grid, format)` | The screen as an image, with a one-line header |
| `save_screen(path, region, max_dim)` | Write the screen to an image file |
| `read_screen_text(region)` | OCR text with positions (needs the `ocr` extra) |
| resource `airplay://screen/latest` | The latest frame as a JPEG, with update notifications |

### Keeping token use down

- `get_screen` always returns the current frame (the last one received if the screen is static). Pass `since_frame=12` to get `unchanged: still frame 12` (about 15 tokens) instead of a repeat image.
- `mode="changes"` sends only the area that changed. A typed digit costs about 90 tokens instead of about 650.
- Letterbox bars are cropped and the status bar is ignored for change detection.
- `region=[x,y,w,h]` zooms in on detail, `max_dim=512` gives a quick glance, and `grid=[9,9]` overlays labelled cells so the model can say "E5".
- Captures wait for animations to settle, so the model isn't sent a blurred mid-transition frame.

Dedupe is tracked per client connection. Stateless HTTP clients get a new connection on every request, so they should pass `since_frame` explicitly.

## Testing without a phone

`--receiver external` (or `--external`, `AIRPLAY_EXTERNAL=1`) skips AirPlay and accepts a raw Annex-B H.264 stream. `start_receiver` reports the port:

```sh
ffmpeg -re -i clip.mp4 -an -c:v libx264 -f h264 tcp://127.0.0.1:<port>
```

## Development

```sh
uv run pytest
npx @modelcontextprotocol/inspector uv run airplay-mcp --external
```

```
iPhone --AirPlay--> pylibairplay receiver --Annex-B H.264 over local TCP--> PyAV decoder
                                                                          |
                                     MCP tools / resource  <--  FrameStore + change detection
```

The receiver is a pluggable interface (`receivers/base.py`; `native.py`, `uxplay.py` and `gstreamer.py` implement it). Android has no practical equivalent: Google certifies Cast receivers, so scrcpy (over ADB) or Miracast are the realistic routes.
