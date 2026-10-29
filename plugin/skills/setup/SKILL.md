---
name: setup
description: One-time setup for the AirPlay Screen plugin. Installs uv, then either extracts the FairPlay files from the user's own iPhone firmware with ipsw or installs UxPlay, and checks that the receiver starts. Use when the user asks to set up AirPlay, when the airplay MCP server fails to start, or when the phone pairs but no video arrives.
---

# AirPlay Screen setup

This plugin runs `airplay-mcp`, a local AirPlay receiver. The phone mirrors to it over the local network and Claude gets the frames. Before it can show video, two things are needed:

1. **uv**, which runs the server (`uvx`).
2. **One of these receivers:**
   - **native** (default). Recent iOS needs Apple's FairPlay handshake, so the server runs the FairPlay code from the **user's own iPhone firmware** in a local emulator. The files are extracted once with `ipsw` and never leave the machine. This works on any OS but needs a multi-GB download.
   - **uxplay**: the [UxPlay](https://github.com/FDH2/UxPlay) program (GPL-3), which handles FairPlay itself. It is easy on macOS and Linux. On Windows it has to be built with MSYS2, which takes about 20 minutes.

Work through the steps below in order. Before installing anything or downloading the firmware, tell the user what you are about to run. Do not commit or upload the extracted firmware files anywhere.

## 1. Check the basics

- The computer and the phone must be on the **same Wi-Fi**. VPNs, guest networks and cloud VMs block mDNS, and then the phone won't see the receiver.
- Check that `uvx --version` works. If it doesn't, install [uv](https://docs.astral.sh/uv/) with the system package manager:
  - macOS: `brew install uv`
  - Windows: `winget install --id=astral-sh.uv -e`
  - Linux: the distro's `uv` package if it has one, otherwise `pipx install uv`

  If none of these is available, send the user to https://docs.astral.sh/uv/getting-started/installation/ to install it themselves. Then tell the user to restart Claude Code so the MCP server can find `uvx`.

## 2. Pick a receiver

Ask the user which receiver they want. Explain the trade-off above, and recommend `uxplay` on macOS and Linux if they would rather skip the firmware download.

## 3a. native: extract the FairPlay files

1. Install [ipsw](https://github.com/blacktop/ipsw):
   - macOS: `brew install blacktop/tap/ipsw`
   - Windows or Linux: download the latest release archive for the platform from https://github.com/blacktop/ipsw/releases, unpack it and put `ipsw` on `PATH` (or call it by its full path).
2. Get the phone's model identifier, for example `iPhone17,3`. Ask the user for the model number shown in Settings > General > About, or the marketing name, and look it up with `ipsw device-list`.
3. Run these in a POSIX shell (Git Bash on Windows) inside `${CLAUDE_PLUGIN_DATA}/airplay-fp`, replacing `MODEL` with the identifier. The download is several GB, so run it in the background and tell the user it will take a while.
   ```sh
   ipsw download ipsw --device MODEL --latest
   ipsw extract --dyld --dyld-arch arm64e MODEL_*.ipsw -o fw
   ipsw dyld extract fw/*/dyld_shared_cache_arm64e -o fpdir AirPlayReceiver AirPlaySupport CoreUtils AirPlaySender
   ```
   If the last command matches nothing, find the cache with `ls fw/*`.
4. Check that `fpdir/` holds four files, then delete `fw/` and the `.ipsw` to free the space.
5. Tell the user to open `/plugin`, choose **airplay-mcp** and configure it: **Receiver** = `native`, **FairPlay folder** = the absolute path of `${CLAUDE_PLUGIN_DATA}/airplay-fp/fpdir`. Then restart Claude Code.

## 3b. uxplay: install UxPlay 1.73 or later

- macOS: `brew install uxplay`
- Debian/Ubuntu: `sudo apt install uxplay`. If `uxplay -v` reports a version older than 1.73, build it from source following the UxPlay README.
- Windows: follow the MSYS2 steps in the [airplay-mcp README](https://github.com/feedthedogs/airplay-mcp#uxplay). Afterwards:
  - add `C:\msys64\ucrt64\bin` to the user's `PATH`
  - allow `uxplay.exe` through the firewall
  - set the Wi-Fi profile to **Private**

Then tell the user to configure the plugin in `/plugin`: **Receiver** = `uxplay`, plus **UxPlay executable** if `uxplay` is not on `PATH`. Then restart Claude Code.

## 4. Check it works

After the restart:

1. Call `start_receiver`.
2. Ask the user to open **Control Centre > Screen Mirroring > "AI Screen"** on the phone (PIN `3939` if asked). The first native connection takes about 35 seconds while the emulator warms up.
3. Call `wait_for_change`, then `get_screen`, and describe what you see.

If it fails:

- **The phone doesn't list the receiver.** It's a network or firewall problem; see step 1.
- **The phone pairs but no video arrives (native).** The FairPlay folder is wrong or incomplete.
- **To see the receiver's log,** run outside the client: `uvx airplay-mcp --autostart -v` (add `--fp-emu-dir <path>` or `--receiver uxplay`).
