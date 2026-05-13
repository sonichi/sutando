#!/usr/bin/env python3
"""Screen recording via macOS screencapture -v. Stores PID in a file for stop/status."""

import subprocess
import signal
import sys
import os
import time
import json

PID_FILE = "/tmp/sutando-screen-record.pid"
INDICATOR_PID_FILE = "/tmp/sutando-rec-indicator.pid"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _show_indicator():
    """Show a macOS notification and a persistent menu bar 'REC' indicator."""
    subprocess.Popen(
        ["osascript", "-e", 'display notification "Screen recording started" with title "Sutando"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    indicator_bin = os.path.join(SCRIPT_DIR, "rec-indicator")
    if os.path.exists(indicator_bin):
        proc = subprocess.Popen(
            [indicator_bin],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(INDICATOR_PID_FILE, "w") as f:
            f.write(str(proc.pid))
        print(json.dumps({"_log": "rec_indicator_started", "pid": proc.pid}), file=sys.stderr)
    else:
        print(json.dumps({"_log": "rec_indicator_missing", "path": indicator_bin}), file=sys.stderr)


def _hide_indicator():
    """Remove menu bar indicator and show stop notification."""
    if os.path.exists(INDICATOR_PID_FILE):
        with open(INDICATOR_PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
            print(json.dumps({"_log": "rec_indicator_stopped", "pid": pid}), file=sys.stderr)
        except ProcessLookupError:
            print(json.dumps({"_log": "rec_indicator_already_dead", "pid": pid}), file=sys.stderr)
        os.remove(INDICATOR_PID_FILE)
    else:
        print(json.dumps({"_log": "rec_indicator_no_pid_file"}), file=sys.stderr)
    subprocess.Popen(
        ["osascript", "-e", 'display notification "Screen recording stopped" with title "Sutando"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _list_audio_devices():
    """Parse avfoundation -list_devices output → list of (index, name) tuples for audio devices.
    Returns [] if no devices or ffmpeg fails."""
    try:
        result = subprocess.run(
            ["/opt/homebrew/bin/ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=5,
        )
        out = result.stderr
        if "AVFoundation audio devices:" not in out:
            return []
        audio_section = out.split("AVFoundation audio devices:", 1)[1].split("Error", 1)[0]
        # Lines look like: "[AVFoundation indev @ 0x...] [N] DeviceName"
        import re as _re
        devices = []
        # Format: "[AVFoundation indev @ 0x...] [N] DeviceName"
        for m in _re.finditer(r"\[AVFoundation[^\]]*\]\s*\[(\d+)\]\s*([^\n\r]+)", audio_section):
            devices.append((int(m.group(1)), m.group(2).strip()))
        return devices
    except Exception:
        return []


def _has_audio_device():
    """Check if avfoundation reports any audio device. Headless Macs (no mic) have none."""
    return len(_list_audio_devices()) > 0


def _pick_audio_device():
    """Pick the best audio device index for recording.

    Priority (Chi 2026-05-13 silence-recording diagnosis):
    1. Built-in MacBook mic — matches /MacBook.*Microphone/. This is the right default
       for solo demos. Virtual devices (ZoomAudioDevice, Microsoft Teams Audio, BlackHole)
       carry silence when their host app isn't actively streaming audio.
    2. Fall back to first non-virtual device (skip known-virtual names).
    3. Last resort: device index 0 (the system "default" — may still be virtual).
    4. Returns None if no audio devices present.

    For Zoom-call recording specifically, set RECORD_AUDIO=ZoomAudioDevice (or its
    index) explicitly. The picker doesn't try to guess from process state because
    "zoom is running" ≠ "in an active call routing audio".
    """
    devices = _list_audio_devices()
    if not devices:
        return None

    builtin_mic_idx = None
    first_real_idx = None
    virtual_keywords = ("zoomaudio", "microsoftteams", "teamsaudio", "blackhole", "aggregate")
    for idx, name in devices:
        lname = name.lower().replace(" ", "")
        if "macbook" in lname and "microphone" in lname:
            builtin_mic_idx = idx
        elif not any(v in lname for v in virtual_keywords) and first_real_idx is None:
            first_real_idx = idx

    if builtin_mic_idx is not None:
        return builtin_mic_idx
    if first_real_idx is not None:
        return first_real_idx
    return devices[0][0]  # all virtual, fall back to first listed


def start():
    if os.path.exists(PID_FILE):
        with open(PID_FILE) as f:
            info = json.load(f)
        try:
            os.kill(info["pid"], 0)
            print(json.dumps({"status": "already_recording", "path": info["path"], "pid": info["pid"]}))
            return
        except ProcessLookupError:
            os.remove(PID_FILE)

    path = f"/tmp/sutando-recording-{int(time.time())}.mov"

    # Detect audio device — headless Macs (no mic) have none, ffmpeg fails if we ask for audio.
    # Pick a real input over the "default" alias (which on machines with Zoom installed maps
    # to ZoomAudioDevice and captures silence when Zoom isn't running). Per Chi 2026-05-13.
    screen = os.environ.get('RECORD_SCREEN', 'Capture screen 0')
    audio_env = os.environ.get('RECORD_AUDIO', '')
    if audio_env:
        # Explicit override: RECORD_AUDIO=none for video-only, or specify a device (name or index)
        input_spec = screen if audio_env == 'none' else f"{screen}:{audio_env}"
    else:
        picked = _pick_audio_device()
        input_spec = f"{screen}:{picked}" if picked is not None else screen

    # Use ffmpeg instead of screencapture -v (which requires TTY).
    # Capture stderr to a sibling log file so audio-acquisition errors are
    # visible after the fact. Per Chi 2026-05-13: silent recordings were
    # untraceable because the previous DEVNULL-stderr discarded ffmpeg's
    # mic-conflict / permission / format errors. With this log the silence
    # guard from PR #667 can point to a root cause instead of just warning.
    log_path = path + ".ffmpeg.log"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        ["/opt/homebrew/bin/ffmpeg", "-f", "avfoundation",
         "-i", input_spec,
         "-r", "15", "-pix_fmt", "yuv420p", "-y", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=log_fh,
    )

    with open(PID_FILE, "w") as f:
        json.dump({"pid": proc.pid, "path": path, "started": time.time(), "log_path": log_path, "input_spec": input_spec}, f)

    _show_indicator()
    print(json.dumps({"status": "recording", "path": path, "pid": proc.pid}))


def stop():
    if not os.path.exists(PID_FILE):
        print(json.dumps({"status": "not_recording"}))
        return

    with open(PID_FILE) as f:
        info = json.load(f)

    try:
        os.kill(info["pid"], signal.SIGINT)
    except ProcessLookupError:
        pass

    for _ in range(10):
        time.sleep(0.5)
        try:
            os.kill(info["pid"], 0)
        except ProcessLookupError:
            break

    os.remove(PID_FILE)
    _hide_indicator()
    path = info["path"]
    exists = os.path.exists(path)
    size = os.path.getsize(path) if exists else 0

    # Audio-silence guard (Chi 2026-05-13: a recording came back at -91 dB pure
    # digital silence even though _pick_audio_device returned the right mic.
    # Transient mic-acquisition failure — ffmpeg started, captured nothing on the
    # audio stream, finished cleanly. Without a check, the user gets a "stopped"
    # status and discovers silence in QuickTime). Run ffmpeg volumedetect on the
    # finished file; if mean_volume is below the silence floor, warn in the
    # return JSON so the caller (voice agent / CLI / web UI) can surface it.
    audio_warning = None
    if exists and size > 1024:  # skip vanishingly-small files
        try:
            r = subprocess.run(
                ["/opt/homebrew/bin/ffmpeg", "-i", path, "-af", "volumedetect", "-vn", "-f", "null", "/dev/null"],
                capture_output=True, text=True, timeout=10,
            )
            mean_db = None
            for line in r.stderr.splitlines():
                if "mean_volume:" in line:
                    try:
                        mean_db = float(line.split("mean_volume:", 1)[1].strip().split()[0])
                    except (ValueError, IndexError):
                        pass
                    break
            if mean_db is not None and mean_db < -80.0:
                audio_warning = f"audio captured but silent (mean {mean_db:.1f} dB) — likely transient mic-acquisition failure; retry the recording or check input device level"
        except Exception:
            pass  # don't fail stop() on guard issues

    out = {"status": "stopped", "path": path, "exists": exists, "size_mb": round(size / 1024 / 1024, 1)}
    if audio_warning:
        out["audio_warning"] = audio_warning
        # When the guard fires, scan the ffmpeg stderr log for known audio-failure
        # signatures so we point at root cause (mic-conflict / permission / format)
        # instead of just saying "it was silent." Per Chi 2026-05-13 deeper audit.
        ffmpeg_log = info.get("log_path")
        if ffmpeg_log and os.path.exists(ffmpeg_log):
            try:
                with open(ffmpeg_log) as f:
                    log_text = f.read()
                signature_hits = []
                # Common avfoundation/CoreAudio failure modes
                for sig, hint in [
                    ("Permission denied", "macOS Microphone permission denied for ffmpeg"),
                    ("Cannot find audio device", "audio device index/name not found (device list may have shifted)"),
                    ("device or resource busy", "device locked by another process (likely voice-agent or Zoom)"),
                    ("kAudioHardwareNotRunningError", "CoreAudio not running — try restarting coreaudiod"),
                    ("Inappropriate ioctl", "audio device acquired in incompatible mode (HAL exclusive vs shared)"),
                    ("Operation not permitted", "macOS sandbox or TCC denial on audio device"),
                ]:
                    if sig in log_text:
                        signature_hits.append(hint)
                if signature_hits:
                    out["audio_root_cause"] = "; ".join(signature_hits)
                out["ffmpeg_log"] = ffmpeg_log
            except Exception:
                pass
        if info.get("input_spec"):
            out["input_spec"] = info["input_spec"]
    print(json.dumps(out))


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        start()
    elif action == "stop":
        stop()
    else:
        print(f"Usage: {sys.argv[0]} [start|stop]")
