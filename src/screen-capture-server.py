#!/usr/bin/env python3
from __future__ import annotations
"""
Screen capture HTTP server — runs in a terminal (has Screen Recording permission).
The voice agent calls http://localhost:7845/capture to get instant screenshots.

Usage: python3 src/screen-capture-server.py
(Run in a terminal window — NOT as a launchd daemon)
"""

import http.server
import subprocess
import json
import os
import secrets
import signal
import stat
import threading
import urllib.request
import os as _os
from datetime import datetime

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
_active_recording = None  # {"proc": Popen, "path": str, "watchdog": threading.Timer, "tap": Popen|None, "mic": Popen|None, "video_path": str}
_recording_lock = threading.Lock()
MAX_RECORDING_SECONDS = 600  # safety cap for a recording nobody stopped

# --- system-audio process tap (issue #2314) ----------------------------------
# System audio is captured with a Core Audio process tap (src/audio-tap/), NOT
# by re-routing output through a BlackHole Multi-Output device. The tap reads
# the output stream in place: speakers stay the default output, volume keys
# keep working, and the audio is lossless. The helper is built on demand.
TAP_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio-tap", "sys-audio-tap")
TAP_BUILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio-tap", "build-audio-tap.sh")


def _ensure_tap_binary() -> bool:
    """Build the tap helper if missing (swiftc, ~5s, once per checkout)."""
    if os.path.exists(TAP_BIN):
        return True
    try:
        subprocess.run(["bash", TAP_BUILD], timeout=120, capture_output=True)
    except Exception:
        return False
    return os.path.exists(TAP_BIN)


def _spawn_audio_captures(audio: str, base: str):
    """Start system-tap and/or mic capture for mode `audio` (mix|system|mic).
    Returns (tap_proc|None, mic_proc|None, fallback_to_legacy_mic: bool).
    A tap that dies within its first ~0.7s (TCC denied / unsupported) triggers
    graceful fallback to the legacy default-input path."""
    tap = mic = None
    if audio in ("mix", "system") and _ensure_tap_binary():
        try:
            tap = subprocess.Popen([TAP_BIN, base + "-sys.wav"],
                                   stderr=subprocess.DEVNULL)
            threading.Event().wait(0.7)
            if tap.poll() is not None:  # died instantly → no permission
                tap = None
        except Exception:
            tap = None
    if audio == "mix" and tap is not None:
        try:
            mic = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "avfoundation", "-i", ":default", base + "-mic.wav"],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            threading.Event().wait(0.5)
            if mic.poll() is not None:
                mic = None
        except Exception:
            mic = None
    # tap unavailable → legacy `screencapture -g` mic path so audio isn't lost
    return tap, mic, (audio in ("mix", "system") and tap is None)


def _finalize_recording(rec) -> str:
    """Stop all capture processes and mux audio into the final clip.
    Returns the path of the finished .mov. Shared by action=stop and the
    watchdog so both produce identical results."""
    for key, sig_ in (("proc", signal.SIGINT), ("tap", signal.SIGINT), ("mic", signal.SIGINT)):
        p = rec.get(key)
        if p is not None:
            try:
                p.send_signal(sig_)
            except Exception:
                pass
    for key, tmo in (("proc", 30), ("tap", 10), ("mic", 10)):
        p = rec.get(key)
        if p is not None:
            try:
                p.wait(timeout=tmo)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    video, final = rec["video_path"], rec["path"]
    if video == final:  # legacy single-process path — nothing to mux
        return final
    sys_wav, mic_wav = final[:-4] + "-sys.wav", final[:-4] + "-mic.wav"
    inputs, have = [], []
    for w in (sys_wav, mic_wav):
        if os.path.exists(w) and os.path.getsize(w) > 44:  # >WAV header
            inputs += ["-i", w]
            have.append(w)
    try:
        if have:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                   "-i", video] + inputs
            if len(have) == 2:
                cmd += ["-filter_complex",
                        "[1:a][2:a]amix=inputs=2:duration=longest[a]",
                        "-map", "0:v", "-map", "[a]"]
            else:
                cmd += ["-map", "0:v", "-map", "1:a"]
            cmd += ["-c:v", "copy", "-c:a", "aac", final]
            subprocess.run(cmd, timeout=120, check=True, capture_output=True)
            os.unlink(video)
        else:  # no audio captured — ship the silent video rather than nothing
            os.replace(video, final)
    except Exception:
        # mux failed: fall back to the raw video so the clip isn't lost
        if os.path.exists(video) and not os.path.exists(final):
            os.replace(video, final)
    for w in have:
        try:
            os.unlink(w)
        except Exception:
            pass
    return final


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
    """Fire a macOS notification that Sutando captured the screen. Chi's ask
    per 2026-04-18 Discord: "shall we use a notification when taking
    screenshots?". Uses osascript (no additional deps). Debounced to
    avoid notification-center spam during describe_screen loops."""
    try:
        import subprocess as _sp
        _sp.run(
            ["osascript", "-e",
             'display notification "Captured screen" with title "Sutando"'],
            timeout=1.0,
            capture_output=True,
        )
    except Exception:
        pass  # Best-effort; notification absence is never critical.


def _notify_capture():
    """Debounced fire-and-forget macOS notification."""
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

    def do_GET(self):
        if self.path.startswith("/capture") and not self.path.startswith("/capture-video"):
            # Reject if no valid token — a browser page on loopback cannot set a
            # custom header on a no-cors fetch, so this is a same-origin CSRF guard.
            provided = self.headers.get("X-Sutando-Capture-Token", "")
            if not CAPTURE_TOKEN or not provided or not secrets.compare_digest(provided, CAPTURE_TOKEN):
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"error","error":"forbidden"}')
                return
            # Parse display number from query: /capture?display=2 or /capture?all=true
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(self.path).query)
            # silent=true suppresses the menu-bar flash + notification. Used by
            # the vision streaming ticker, which fires once per second and would
            # otherwise spam the indicator and notification center.
            silent = query.get("silent", ["false"])[0] == "true"
            if not silent:
                # Flash agent-state=seeing on the menu-bar avatar for ~1.5s.
                # Non-blocking fire-and-forget; capture succeeds regardless.
                _signal_seeing()
                # macOS notification "Sutando captured screen" — opt-out via
                # SUTANDO_CAPTURE_NOTIFY=0. Debounced at 5s to avoid spam
                # during burst captures.
                _notify_capture()
            os.makedirs(DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            display_raw = query.get("display", [None])[0]
            # Coerce to int to short-circuit taint flow into the subprocess
            # argument list. Display index constrained to 1..9 (macOS never has
            # more than a handful of displays).
            display = int(display_raw) if display_raw and display_raw.isdigit() and 1 <= int(display_raw) <= 9 else None
            capture_all = query.get("all", ["false"])[0] == "true"
            # format=jpeg → screencapture -t jpg, smaller files for streaming.
            fmt = query.get("format", ["png"])[0]
            if fmt not in ("png", "jpg", "jpeg"):
                fmt = "png"
            ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
            type_flag = "jpg" if ext == "jpg" else "png"
            try:
                if capture_all:
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
                else:
                    suffix = f"-d{display}" if display else ""
                    path = f"{DIR}/screen-{ts}{suffix}.{ext}"
                    paths = [path]
                    cmd = ["screencapture", "-x", "-t", type_flag]
                    if display:
                        cmd.append(f"-D{display}")
                    cmd.append(path)
                    subprocess.run(cmd, timeout=5, check=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                resp = {"status": "ok", "path": paths[0] if paths else path}
                if len(paths) > 1:
                    resp["all_paths"] = paths
                    resp["displays"] = len(paths)
                self.wfile.write(json.dumps(resp).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": str(e)}).encode())
        elif self.path.startswith("/capture-video"):
            # Record a short screen video and return the .mov path. Backs the
            # ⌃R "Drop Video Clip" hotkey — a few-second video repro is far easier
            # to act on than a single still. Recording runs through THIS server
            # (not Sutando.app directly) because this process holds the Screen
            # Recording TCC grant, same reason /capture does.
            #
            # Token gate FIRST — before any side effect (no flash, no recording)
            # so an unauthorized request can't even signal. See CAPTURE_TOKEN.
            supplied = self.headers.get("X-Sutando-Capture-Token", "")
            if not CAPTURE_TOKEN or not secrets.compare_digest(supplied, CAPTURE_TOKEN):
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "error": "forbidden"}).encode())
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
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "idle"}).encode())
                    return
                try:
                    if rec.get("watchdog"):
                        rec["watchdog"].cancel()
                    path = _finalize_recording(rec)  # stops video+tap+mic, muxes
                    # Only publish OFF if no NEW recording started during this
                    # finalization (SIGINT+wait released the lock). Otherwise the
                    # stale stop would stomp the new recording's ON state, leaving
                    # the app's isRecordingVideo=false while it records. (CR: qingyun-wu)
                    with _recording_lock:
                        if _active_recording is None:
                            _post_recording_state(False)
                    if not (os.path.exists(path) and os.path.getsize(path) > 0):
                        raise RuntimeError("recording produced no file")
                    if query.get("silent", ["false"])[0] != "true":
                        _signal_seeing()
                        _notify_capture()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok", "path": path}).encode())
                except Exception as e:
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "error", "error": str(e)}).encode())
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
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "already_recording", "path": _active_recording["path"]}).encode())
                        return
                    if query.get("silent", ["false"])[0] != "true":
                        _signal_seeing()
                        _notify_capture()
                    # Audio (issue #2314): default `mix` = system audio via the
                    # process tap (src/audio-tap/) + mic via ffmpeg, muxed at stop.
                    # Output device is untouched → volume keys work while recording.
                    #   ?audio=mix     system + mic (default; meetings/narration)
                    #   ?audio=system  system audio only (screen demos)
                    #   ?audio=mic     legacy default-input path (-g / -G<id>)
                    #   ?audio=off     silent
                    # `on` is accepted as an alias of `mix` for old callers.
                    # If the tap can't run (TCC denied, unbuilt, pre-14.2 macOS)
                    # we fall back to the legacy -g path so audio isn't lost.
                    audio = query.get("audio", ["mix"])[0]
                    if audio == "on":
                        audio = "mix"
                    device = query.get("device", [None])[0]
                    tap = mic_proc = None
                    video_path = path
                    if audio in ("mix", "system"):
                        tap, mic_proc, fallback = _spawn_audio_captures(audio, path[:-4])
                        if fallback:
                            audio = "mic"
                        else:
                            video_path = path[:-4] + "-video.mov"
                    cmd = ["screencapture", "-v", "-x"]  # no -V → records until SIGINT
                    if audio == "mic":
                        cmd.append(f"-G{device}" if device else "-g")
                    if display:
                        cmd.append(f"-D{display}")
                    cmd.append(video_path)
                    try:
                        proc = subprocess.Popen(cmd)
                    except Exception as e:
                        for p in (tap, mic_proc):  # don't leak audio captures
                            if p is not None:
                                try:
                                    p.kill()
                                except Exception:
                                    pass
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"status": "error", "error": str(e)}).encode())
                        return

                    def _auto_stop(p=proc):
                        global _active_recording
                        # Publish OFF only if this watchdog still OWNS the active
                        # recording, atomically under the lock — a stale watchdog
                        # (its proc already replaced by a newer recording) must not
                        # stomp the newer recording's ON state. (CR: qingyun-wu)
                        rec = None
                        with _recording_lock:
                            if _active_recording and _active_recording.get("proc") is p:
                                rec = _active_recording
                                _active_recording = None
                                _post_recording_state(False)
                        if rec:
                            try:
                                _finalize_recording(rec)
                            except Exception:
                                pass

                    # ?max=<seconds> raises the safety cap for known-long sessions
                    # (meeting-link auto-record needs meeting-length clips; the
                    # 600s default ate a 41-min meeting on 2026-07-22). Bounded
                    # at 4h so a typo can't disable the watchdog entirely.
                    max_raw = query.get("max", [None])[0]
                    cap = MAX_RECORDING_SECONDS
                    if max_raw and max_raw.isdigit() and int(max_raw) > 0:
                        cap = min(int(max_raw), 4 * 3600)
                    wd = threading.Timer(cap, _auto_stop)
                    wd.daemon = True
                    # Register the active recording BEFORE arming the watchdog: a
                    # tiny cap could fire _auto_stop almost immediately, and it must
                    # see _active_recording already set (both run under
                    # _recording_lock, so the callback blocks until this returns).
                    _active_recording = {"proc": proc, "path": path, "watchdog": wd,
                                         "tap": tap, "mic": mic_proc,
                                         "video_path": video_path}
                    wd.start()
                    _post_recording_state(True)  # under the lock: a concurrent stop can't interleave a stale ON (CR: qingyun-wu)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "recording", "path": path}).encode())
                return

            # Toggle-only: /capture-video needs action=start|stop. (The old
            # fixed-duration -V path was removed — ⌃R is a start/stop toggle now.)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "error": "action must be start or stop"}).encode())
        elif self.path == "/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"pong":true}')
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Screen capture server → http://localhost:{PORT}/capture")
    print("Keep this terminal open — it has Screen Recording permission.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")
