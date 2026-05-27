---
name: spotify-control
description: "Control Spotify playback on macOS via voice — play, pause, next, previous, volume. Uses shpotify (AppleScript wrapper around the desktop Spotify app). No OAuth, no API keys."
---

# Spotify Control

Voice-friendly Spotify playback control via the local desktop Spotify app. Uses [shpotify](https://harishnarayanan.org/projects/shpotify/) under the hood — a bash wrapper around Spotify's AppleScript interface. Same pattern as Sutando's existing AppleScript-based skills (reminders, iMessage, calendar).

## When to Use

- **Playback**: "Play", "pause", "next track", "previous track", "stop"
- **Volume**: "Volume up", "volume down", "set volume to 50"
- **Search + play**: "Play [artist/song/album] on Spotify"
- **Now playing**: "What's playing?", "what song is this?"

## Tools

All scripts under `scripts/`. Each is a thin wrapper around the `spotify` CLI from shpotify.

| Command | Script |
|---|---|
| `bash scripts/play.sh` | Resume playback |
| `bash scripts/pause.sh` | Pause playback |
| `bash scripts/next.sh` | Skip to next track |
| `bash scripts/prev.sh` | Back to previous track |
| `bash scripts/now-playing.sh` | Echo current track + artist |
| `bash scripts/volume.sh <0-100\|up\|down>` | Adjust volume |
| `bash scripts/search-play.sh "<query>"` | Search + play first match |

## Installation

Run once per host:

```bash
bash skills/spotify-control/install.sh
```

This installs shpotify via Homebrew if it's not already present.

## Requirements

- macOS (uses AppleScript)
- Spotify desktop app installed and logged in (free or paid account both work)
- Homebrew installed (for `shpotify` package)

## Latency

Under 1 second from voice command to playback action. AppleScript is local — no network round-trip.

## Limitations

- Controls only the local desktop Spotify app, not Spotify Connect on phones/speakers. For cross-device control, use `spotify-cli` (separate skill, requires OAuth).
- Search-play returns the first match — for ambiguous queries the result can be off. v2 could add a confirmation step.
