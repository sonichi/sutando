---
name: screen-record
description: Start or stop a screen recording of the Mac via ffmpeg. Use when the user asks to record/capture their screen to a video file (a demo, a repro, a walkthrough) — not for a single still screenshot (use macos-tools screen capture for that).
---

# Screen Record

Start and stop screen recording using ffmpeg.

**Usage**: `/screen-record start` or `/screen-record stop`

## When to use

The user asks to **record** the screen to a video over time (demo, bug repro,
walkthrough). For a single still frame, use the screen-capture path instead.

## On activation

If argument is `start`:
```bash
python3 skills/screen-record/scripts/record.py start
```

If argument is `stop`:
```bash
python3 skills/screen-record/scripts/record.py stop
```

## Output & failure modes
- **Done =** a `.mov` written by `record.py` to `/tmp/sutando-recording-<ts>.mov` (it prints the path on stop).
- `start` while already recording → the script no-ops and returns the existing pid + path; a **stale** PID file (previous run crashed) is detected and cleared, so it self-heals rather than wedging; `stop` with nothing running → no-op.
- ffmpeg missing at `/opt/homebrew/bin/ffmpeg` (hardcoded in **three** places — `record.py:59` device-listing, `:152` the recording call, `:204` the audio check. Apple Silicon brew prefix; on an Intel Mac `brew install ffmpeg` lands in `/usr/local` and the skill stays broken until **all three** are fixed — patching only the recording call still fails at device-listing before recording starts). No frames / black video → the invoking terminal lacks macOS **Screen Recording** permission (same grant the screen-capture server uses).
