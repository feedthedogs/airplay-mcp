---
name: airplay-screen
description: See the user's iPhone or iPad screen. Sets up airplay-mcp, a local AirPlay receiver, registers it as an MCP server, and then reads the mirrored screen frame by frame. Use when the user wants you to look at, follow or save what is on their phone.
version: 0.1.3
metadata:
  openclaw:
    emoji: "📱"
    homepage: https://github.com/feedthedogs/airplay-mcp
    os: [macos, linux, windows]
    requires:
      bins: [uv]
    install:
      - kind: uv
        package: airplay-mcp
        bins: [airplay-mcp]
    envVars:
      - name: AIRPLAY_RECEIVER
        required: false
        description: "native (default, needs AIRPLAY_FP_EMU_DIR) or uxplay"
      - name: AIRPLAY_FP_EMU_DIR
        required: false
        description: Folder with the FairPlay files extracted from the user's own iPhone firmware (native receiver)
      - name: AIRPLAY_UXPLAY_PATH
        required: false
        description: Full path to the uxplay executable if it is not on PATH (uxplay receiver)
      - name: AIRPLAY_NAME
        required: false
        description: Name the phone shows under Screen Mirroring (default "AI Screen")
      - name: AIRPLAY_PIN
        required: false
        description: Pairing PIN if the phone asks for one (default 3939)
---

# AirPlay Screen

[airplay-mcp](https://github.com/feedthedogs/airplay-mcp) (MIT, on PyPI) turns this computer into an AirPlay receiver. The user mirrors their iPhone or iPad to it, and you get the screen through MCP tools. Everything stays on this machine. The receiver only listens on the local network and makes no outbound connections.

**It only works when this machine is on the same Wi-Fi as the phone.** A cloud VM or VPS can't see the phone. Neither can a network behind a VPN or a guest network, because they block mDNS. If that's the setup, tell the user so and stop.

## One-time setup

Tell the user what you are about to install or download before you run each step.

1. **Install the server:** `uv tool install --upgrade "airplay-mcp[fairplay]"`. The `fairplay` extra adds the emulator libraries the native receiver needs. Check it with `airplay-mcp --help`.
2. **Pick a receiver** with the user. Recent iOS sends no video until the receiver passes Apple's FairPlay check, and there are two ways to handle that:
   - **native** (default): run the FairPlay code from **the user's own iPhone firmware** in a local emulator. Any OS, but the firmware download is several GB. The extracted files must never be uploaded or shared.
   - **uxplay**: install [UxPlay](https://github.com/FDH2/UxPlay) 1.73 or later, which handles FairPlay itself. Easy on macOS (`brew install uxplay`) and Debian/Ubuntu (`sudo apt install uxplay`). On Windows it has to be built with MSYS2: follow the [README steps](https://github.com/feedthedogs/airplay-mcp#uxplay).
3. **native only: extract the FairPlay files.**
   1. Install [ipsw](https://github.com/blacktop/ipsw): `brew install blacktop/tap/ipsw` on macOS. Elsewhere, download a release build from GitHub and put it on `PATH`.
   2. Ask the user for the phone model (Settings > General > About) and map it to an identifier such as `iPhone17,3` with `ipsw device-list`.
   3. In a POSIX shell, in a folder the user wants to keep (for example `~/airplay-fp`), run the commands below. Replace `MODEL` with the identifier.
      ```sh
      ipsw download ipsw --device MODEL --latest
      ipsw extract --dyld --dyld-arch arm64e MODEL_*.ipsw -o fw
      ipsw dyld extract fw/*/dyld_shared_cache_arm64e -o fpdir AirPlayReceiver AirPlaySupport CoreUtils AirPlaySender
      ```
   4. Check that `fpdir/` holds four files, then delete `fw/` and the `.ipsw`.
4. **Register the MCP server** with an absolute path:
   - native:
     ```sh
     openclaw mcp set airplay '{"command":"airplay-mcp","env":{"AIRPLAY_FP_EMU_DIR":"/absolute/path/to/airplay-fp/fpdir"}}'
     ```
   - uxplay:
     ```sh
     openclaw mcp set airplay '{"command":"airplay-mcp","env":{"AIRPLAY_RECEIVER":"uxplay"}}'
     ```
     Add `AIRPLAY_UXPLAY_PATH` if `uxplay` isn't on `PATH`.

   Confirm it with `openclaw mcp list`. The tools appear in a **new session**, or after the gateway reloads.
5. **Firewall:** the first time the receiver runs, allow it through the firewall. The phone connects over TCP and mDNS (UDP 5353).

## Using it

1. `start_receiver`, then tell the user: *Control Centre > Screen Mirroring > "AI Screen"* (PIN `3939` if asked). With native, the first connection takes about 35 seconds while the emulator warms up.
2. `wait_for_change` blocks until the first frame arrives, and afterwards until the screen next changes. Pass `since_frame=N` for the frame you hold.
3. `get_screen` to look, or `save_screen` to write a full-resolution file and return only its path. Use `mode='changes'` after the user acts, and `region` to read small detail. `read_screen_text` is cheaper for text-heavy screens, if it is installed.
4. Repeat 2 and 3, then `stop_receiver` when done.

## Troubleshooting

- **The phone doesn't list "AI Screen".** Check the network and the firewall (see above).
- **It pairs but the screen stays blank (native).** `AIRPLAY_FP_EMU_DIR` is wrong or incomplete, or the `fairplay` extra is missing.
- **To see the log:** `airplay-mcp --autostart -v` (plus `--fp-emu-dir <path>` or `--receiver uxplay`).
