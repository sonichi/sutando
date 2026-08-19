#!/usr/bin/env python3
from __future__ import annotations
"""
Screen capture HTTP server — runs in a terminal (has Screen Recording permission
on macOS; needs no special setup on Windows).
The voice agent calls http://localhost:7845/capture to get instant screenshots.

Usage: python3 src/screen-capture-server.py
(On macOS: run in a terminal window — NOT as a launchd daemon, because the
terminal app holds the Screen Recording TCC grant.)
"""

import http.server
import subprocess
import json
import os
import secrets
import signal
import stat
import sys
import threading
import urllib.request
import os as _os
from datetime import datetime
from pathlib import Path

# Cross-platform OS helpers. `sutando_platform.notify` + `sutando_platform.capture_screen`
# branch on sys.platform so the legacy macOS code paths stay verbatim.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sutando_platform import capture_screen as _platform_capture_screen, notify as _platform_notify, is_macos, is_windows  # noqa: E402

PORT = 7845
# Per-user temp dir — same treatment as browser.mjs in this PR: a shared
# /tmp/sutando-screenshots is owned by whichever account wrote it first and
# EACCES-fails the second account. SUTANDO_SCREENSHOT_DIR overrides.
import tempfile as _tempfile
DIR = _os.environ.get("SUTANDO_SCREENSHOT_DIR") or _os.path.join(
    _tempfile.gettempdir(), "sutando-screenshots")
# Web-client endpoint for agent-state reporting. When a /capture happens we
# flash state=seeing on the menu-bar avatar for ~1.5s — makes screen-capture
# visible to the user without them needing to watch the web UI.
WEB_CLIENT_STATE_URL = "http://localhost:8080/mute-state?state=seeing&ttl_ms=1500&source=tool"

# Shared token for /capture and any future side-effectful endpoints.
# Generated once at startup and stored 0600 so only the owning user can read it.
# Callers must include it in the X-Sutando-Capture-Token header; a browser page
# cannot read a local file or set a custom header on a no-cors request, so it
# cannot reach these endpoints even if the server is on loopback.
_CAPTURE_TOKEN_PATH = _os.path.expanduser("~/.config/sutando/screen-capture-token")


def _load_or_create_capture_token() -> str | None:
    try:
        if _os.path.lexists(_CAPTURE_TOKEN_PATH):
            st = _os.lstat(_CAPTURE_TOKEN_PATH)
            if (stat.S_ISREG(st.st_mode) and (st.st_mode & 0o777) == 0o600
                    and st.st_uid == _os.getuid()):
                with open(_CAPTURE_TOKEN_PATH) as _f:
                    existing = _f.read().strip()
                if existing:
                    return existing
            _os.unlink(_CAPTURE_TOKEN_PATH)
        _os.makedirs(_os.path.dirname(_CAPTURE_TOKEN_PATH), exist_ok=True)
        tok = secrets.token_urlsafe(32)
        fd = _os.open(_CAPTURE_TOKEN_PATH,
                      _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | _os.O_NOFOLLOW, 0o600)
        _os.write(fd, tok.encode())
        _os.close(fd)
        return tok
    except Exception:
        return None


CAPTURE_TOKEN = _load_or_create_capture_token()
# Web-client endpoint for agent-state reporting. When a /capture happens we
# flash state=seeing on the menu-bar avatar for ~1.5s — makes screen-capture
# visible to the user without them needing to watch the web UI.
WEB_CLIENT_STATE_URL = "http://localhost:8080/mute-state?state=seeing&ttl_ms=1500&source=tool"

# macOS notification toggle. Default on; opt out during demo recordings.
NOTIFY_ENABLED = _os.environ.get("SUTANDO_CAPTURE_NOTIFY", "1") != "0"

# Debounce: don't spam notifications for burst captures (e.g. a loop of
# describe_screen calls every 5s). One notification per this many seconds.
NOTIFY_DEBOUNCE_S = 5.0
_last_notify_ts = 0.0

# --- ⌃R start/stop toggle state ---------------------------------------------
# A single open-ended `screencapture -v` recording driven by two ⌃R presses
# (start, then stop). Only one at a time; guarded by _recording_lock. The
# watchdog auto-stops a forgotten recording so it can't run forever / fill disk.
_active_recording = None  # {"proc": Popen, "path": str, "watchdog": threading.Timer}
_recording_lock = threading.Lock()
MAX_RECORDING_SECONDS = 600  # safety cap for a recording nobody stopped


def _post_recording_state(on: bool):
    """Darwin-notify recording state so Sutando.app can flip the Drop Video
    Clip 🔴 without polling (Susan 2026-07-22: the server KNOWS when recording
    starts/stops — push, don't poll). Covers ⌃R toggles, watcher-started
    sessions, and the watchdog auto-stop uniformly. Fire-and-forget."""
    try:
        subprocess.Popen(["notifyutil", "-p",
                          "com.sutando.recording." + ("on" if on else "off")])
    except Exception:
        pass


def _signal_seeing_blocking():
    try:
        req = urllib.request.Request(WEB_CLIENT_STATE_URL, method="GET")
        urllib.request.urlopen(req, timeout=0.3)
    except Exception:
        pass  # Web-client may not be running; that's fine.


def _signal_seeing():
    """True fire-and-forget POST to web-client signaling agent is looking
    at the screen. Spawns a daemon thread so the capture handler isn't
    blocked by web-client latency. Silent on any failure — this is a UI
    signal, not a critical path. Without threading, urllib.request.urlopen
    is synchronous and can block the caller up to the 300ms timeout if the
    web-client is slow (flagged in #428 cold-review)."""
    threading.Thread(target=_signal_seeing_blocking, daemon=True).start()


def _notify_capture_blocking():
    """Fire a desktop notification that Sutando captured the screen. Chi's ask
    per 2026-04-18 Discord: "shall we use a notification when taking
    screenshots?". Routed through `sutando_platform.notify` so the macOS osascript
    backend and the Windows PowerShell balloon-tip backend both work without
    branching here. Debounced to avoid notification-center spam during
    describe_screen loops."""
    try:
        _platform_notify("Captured screen")
    except Exception:
        pass  # Best-effort; notification absence is never critical.


# A frame that could not be recompressed may still pass if it is already
# small; past this it is an error — D7.4 makes the downscale budget
# MANDATORY, and silently sending a native-res original re-creates FE-1.
DOWNSCALE_FAIL_MAX_BYTES = 400 * 1024


def _downscale_frame(path: str, maxdim: int | None, quality: int | None) -> bool:
    """P7 D7.4: resize/recompress a captured frame IN THIS PROCESS via sips.

    Runs before the path is returned to the caller, so the voice event loop
    only ever touches the already-shrunk file. Returns False when the frame
    could not be brought under budget (sips failed AND the original exceeds
    DOWNSCALE_FAIL_MAX_BYTES) — the caller must error, not pass it through."""
    cmd = ["sips"]
    if maxdim:
        cmd += ["--resampleHeightWidthMax", str(maxdim)]
    if quality:
        cmd += ["-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
    cmd.append(path)
    try:
        subprocess.run(cmd, timeout=10, capture_output=True, check=True)
        return True
    except Exception:
        try:
            return os.path.getsize(path) <= DOWNSCALE_FAIL_MAX_BYTES
        except Exception:
            return False


def _notify_capture():
    """Debounced fire-and-forget desktop notification."""
    global _last_notify_ts
    if not NOTIFY_ENABLED:
        return
    import time as _time
    now = _time.time()
    if now - _last_notify_ts < NOTIFY_DEBOUNCE_S:
        return
    _last_notify_ts = now
    threading.Thread(target=_notify_capture_blocking, daemon=True).start()

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _send_json(self, status: int, payload: dict) -> None:
        """Emit the shared JSON response contract for capture routes."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def _require_capture_token(self) -> bool:
        """Fail closed unless the request carries the startup capture token."""
        supplied = self.headers.get("X-Sutando-Capture-Token", "")
        if CAPTURE_TOKEN and supplied and secrets.compare_digest(supplied, CAPTURE_TOKEN):
            return True
        self._send_json(403, {"status": "error", "error": "forbidden"})
        return False

    def do_GET(self):
        if self.path.startswith("/capture") and not self.path.startswith("/capture-video"):
            self._handle_capture()
        elif self.path.startswith("/capture-video"):
            self._handle_capture_video()
        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"pong":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_capture(self) -> None:
        # Reject if no valid token — a browser page on loopback cannot set a
        # custom header on a no-cors fetch, so this is a same-origin CSRF guard.
        if not self._require_capture_token():
            return
        # Parse display number from query: /capture?display=2 or /capture?all=true
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        # silent=true suppresses the menu-bar flash and notification, for
        # callers that capture on a timer.
        silent = query.get("silent", ["false"])[0] == "true"
        if not silent:
            # Flash agent-state=seeing on the menu-bar avatar for ~1.5s.
            # Non-blocking fire-and-forget; capture succeeds regardless.
            _signal_seeing()
            # Opt out with SUTANDO_CAPTURE_NOTIFY=0. Debounced at 5s so a
            # burst of captures raises one notification.
            _notify_capture()
        os.makedirs(DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        display_raw = query.get("display", [None])[0]
        # Coerce to int so the value cannot taint the subprocess argument
        # list; display index is constrained to 1..9.
        display = int(display_raw) if display_raw and display_raw.isdigit() and 1 <= int(display_raw) <= 9 else None
        capture_all = query.get("all", ["false"])[0] == "true"
        # format=jpeg → screencapture -t jpg, smaller files for streaming.
        fmt = query.get("format", ["png"])[0]
        if fmt not in ("png", "jpg", "jpeg"):
            fmt = "png"
        ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
        type_flag = "jpg" if ext == "jpg" else "png"
        # P7 D7.4: optional downscale/recompress executed HERE, in the capture
        # server's process (sips subprocess) — vision compression must never
        # compete with the voice event loop. maxdim bounds the longest edge;
        # quality is JPEG percent (jpg only). Bounded to sane ranges.
        maxdim_raw = query.get("maxdim", [None])[0]
        maxdim = int(maxdim_raw) if maxdim_raw and maxdim_raw.isdigit() and 320 <= int(maxdim_raw) <= 3840 else None
        quality_raw = query.get("quality", [None])[0]
        quality = int(quality_raw) if quality_raw and quality_raw.isdigit() and 10 <= int(quality_raw) <= 100 else None
        try:
            # macOS supports explicit per-display capture. Windows currently
            # falls back to one virtual-screen capture for both `all` and
            # `display` requests.
            if capture_all and is_macos():
                # Capture all displays separately
                paths = []
                for d in range(1, 5):  # up to 4 displays
                    p = f"{DIR}/screen-{ts}-d{d}.{ext}"
                    result = subprocess.run(["screencapture", "-x", "-t", type_flag, f"-D{d}", p], timeout=5, capture_output=True)
                    if result.returncode == 0 and os.path.exists(p) and os.path.getsize(p) > 0:
                        paths.append(p)
                    else:
                        try: os.unlink(p)
                        except Exception: pass
                        break  # no more displays
                path = paths[0] if paths else f"{DIR}/screen-{ts}.{ext}"
            elif is_macos() and display:
                path = f"{DIR}/screen-{ts}-d{display}.{ext}"
                paths = [path]
                cmd = ["screencapture", "-x", "-t", type_flag]
                cmd.append(f"-D{display}")
                cmd.append(path)
                subprocess.run(cmd, timeout=5, check=True)
            else:
                path = os.path.join(DIR, f"screen-{ts}.{ext}")
                paths = [path]
                ok = _platform_capture_screen(path, fmt=ext)
                if not ok:
                    raise RuntimeError(
                        "capture_screen returned False — check Screen Recording "
                        "permission (macOS) or PowerShell availability (Windows)"
                    )
            if maxdim or (quality and ext == "jpg"):
                for p in paths:
                    if not _downscale_frame(p, maxdim, quality if ext == "jpg" else None):
                        for cleanup in paths:
                            try: os.unlink(cleanup)
                            except Exception: pass
                        self._send_json(500, {"status": "error", "error": "downscale failed and frame exceeds budget"})
                        return
            resp = {"status": "ok", "path": paths[0] if paths else path}
            if len(paths) > 1:
                resp["all_paths"] = paths
                resp["displays"] = len(paths)
            self._send_json(200, resp)
        except Exception as e:
            self._send_json(500, {"status": "error", "error": str(e)})

    def _handle_capture_video(self) -> None:
        # Records a screen video and returns the .mov path. Runs here because
        # this process holds the Screen Recording TCC grant.
        #
        # Token gate runs before any side effect, so an unauthorized request
        # produces no flash and no recording.
        if not self._require_capture_token():
            return
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(self.path).query)
        action = query.get("action", [""])[0]
        global _active_recording

        # ⌃R toggle — STOP: SIGINT the running recording so screencapture
        # finalizes the .mov, then return its path. Idempotent if idle.
        if action == "stop":
            with _recording_lock:
                rec = _active_recording
                _active_recording = None
            if not rec:
                self._send_json(200, {"status": "idle"})
                return
            try:
                if rec.get("watchdog"):
                    rec["watchdog"].cancel()
                rec["proc"].send_signal(signal.SIGINT)  # -v finalizes on SIGINT
                rec["proc"].wait(timeout=30)
                path = rec["path"]
                # Publish OFF only if no newer recording started while the lock
                # was released, so the stale stop cannot clear its ON state.
                with _recording_lock:
                    if _active_recording is None:
                        _post_recording_state(False)
                if not (os.path.exists(path) and os.path.getsize(path) > 0):
                    raise RuntimeError("recording produced no file")
                if query.get("silent", ["false"])[0] != "true":
                    _signal_seeing()
                    _notify_capture()
                self._send_json(200, {"status": "ok", "path": path})
            except Exception as e:
                self._send_json(500, {"status": "error", "error": str(e)})
            return

        # ⌃R toggle — START: spawn an open-ended `screencapture -v` (no -V) and
        # return immediately; the second ⌃R (action=stop) ends it.
        if action == "start":
            os.makedirs(DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            display_raw = query.get("display", [None])[0]
            display = int(display_raw) if display_raw and display_raw.isdigit() and 1 <= int(display_raw) <= 9 else None
            suffix = f"-d{display}" if display else ""
            path = f"{DIR}/clip-{ts}{suffix}.mov"
            with _recording_lock:
                if _active_recording:
                    self._send_json(200, {"status": "already_recording", "path": _active_recording["path"]})
                    return
                if query.get("silent", ["false"])[0] != "true":
                    _signal_seeing()
                    _notify_capture()
                # -g records the default input device, so the clip follows the
                # system input; ?audio=off mutes, ?device=<id> pins one via -G.
                audio = query.get("audio", ["on"])[0]
                device = query.get("device", [None])[0]
                cmd = ["screencapture", "-v", "-x"]  # no -V → records until SIGINT
                if audio != "off":
                    cmd.append(f"-G{device}" if device else "-g")
                if display:
                    cmd.append(f"-D{display}")
                cmd.append(path)
                try:
                    proc = subprocess.Popen(cmd)
                except Exception as e:
                    self._send_json(500, {"status": "error", "error": str(e)})
                    return

                def _auto_stop(p=proc):
                    global _active_recording
                    # Publish OFF only while this watchdog still owns the active
                    # recording, so a stale one cannot clear a newer ON state.
                    with _recording_lock:
                        if _active_recording and _active_recording.get("proc") is p:
                            _active_recording = None
                            _post_recording_state(False)
                    try:
                        p.send_signal(signal.SIGINT)
                        p.wait(timeout=30)
                    except Exception:
                        pass

                # ?max=<seconds> raises the cap for known-long sessions, bounded
                # at 4h so a typo cannot disable the watchdog.
                max_raw = query.get("max", [None])[0]
                cap = MAX_RECORDING_SECONDS
                if max_raw and max_raw.isdigit() and int(max_raw) > 0:
                    cap = min(int(max_raw), 4 * 3600)
                wd = threading.Timer(cap, _auto_stop)
                wd.daemon = True
                # Register before arming the watchdog: a small cap can fire
                # _auto_stop at once, and it must see _active_recording set.
                _active_recording = {"proc": proc, "path": path, "watchdog": wd}
                wd.start()
                _post_recording_state(True)  # under the lock: a concurrent stop can't interleave a stale ON (CR: qingyun-wu)
            self._send_json(200, {"status": "recording", "path": path})
            return

        # Toggle-only: /capture-video needs action=start|stop. (The old
        # fixed-duration -V path was removed — ⌃R is a start/stop toggle now.)
        self._send_json(400, {"status": "error", "error": "action must be start or stop"})

if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Screen capture server → http://localhost:{PORT}/capture")
    print("Keep this terminal open — it has Screen Recording permission.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
