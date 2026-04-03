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


def _hide_indicator():
    """Remove menu bar indicator and show stop notification."""
    if os.path.exists(INDICATOR_PID_FILE):
        with open(INDICATOR_PID_FILE) as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.remove(INDICATOR_PID_FILE)
    subprocess.Popen(
        ["osascript", "-e", 'display notification "Screen recording stopped" with title "Sutando"'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


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

    # Use ffmpeg instead of screencapture -v (which requires TTY)
    proc = subprocess.Popen(
        ["ffmpeg", "-f", "avfoundation", "-i", "Capture screen 0:none",
         "-r", "15", "-pix_fmt", "yuv420p", "-y", path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    with open(PID_FILE, "w") as f:
        json.dump({"pid": proc.pid, "path": path, "started": time.time()}, f)

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
    print(json.dumps({"status": "stopped", "path": path, "exists": exists, "size_mb": round(size / 1024 / 1024, 1)}))


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        start()
    elif action == "stop":
        stop()
    else:
        print(f"Usage: {sys.argv[0]} [start|stop]")
