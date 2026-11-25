---
name: setup
description: One-time setup for the AirPlay Screen plugin. Installs uv and checks that the receiver starts. Use when the user asks to set up AirPlay, when the airplay MCP server fails to start, or when the phone pairs but no video arrives.
---

# AirPlay Screen setup

This plugin runs `airplay-mcp`, a local AirPlay receiver. The phone mirrors to it over the local network and Claude gets the frames. The AirPlay protocol and FairPlay are handled in pure Python — no firmware extraction or external programs needed.

## 1. Check the basics

- The computer and the phone must be on the **same Wi-Fi**. VPNs, guest networks and cloud VMs block mDNS, and then the phone won't see the receiver.
- Check that `uvx --version` works. If it doesn't, install [uv](https://docs.astral.sh/uv/) with the system package manager:
  - macOS: `brew install uv`
  - Windows: `winget install --id=astral-sh.uv -e`
  - Linux: the distro's `uv` package if it has one, otherwise `pipx install uv`

  If none of these is available, send the user to https://docs.astral.sh/uv/getting-started/installation/ to install it themselves. Then tell the user to restart Claude Code so the MCP server can find `uvx`.

## 2. Check it works

1. Call `start_receiver`.
2. Ask the user to open **Control Centre > Screen Mirroring > "AI Screen"** on the phone (PIN `3939` if asked).
3. Call `wait_for_change`, then `get_screen`, and describe what you see.

If it fails:

- **The phone doesn't list the receiver.** It's a network or firewall problem; see step 1.
- **To see the receiver's log,** run outside the client: `uvx airplay-mcp --autostart -v`.
