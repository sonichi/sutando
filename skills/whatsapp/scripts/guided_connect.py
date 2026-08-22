#!/usr/bin/env python3
"""Guided WhatsApp pairing: orchestrate `wacli auth` and emit a line protocol
the calling agent relays into chat, so the user never opens a terminal.

The seam: this script owns auth orchestration (spawn, event parsing, QR
rendering, verification); the agent owns delivery (posting PAIR_CODE text or
the QR_PNG file into the user's chat and re-posting on refresh).

Line protocol (stdout, one record per line):
  ALREADY_CONNECTED         session valid; probe passed
  PAIR_CODE: <code>         phone-pairing code — relay as text, user types it in
                            WhatsApp > Linked devices > "Link with phone number"
  QR_PNG: <path>            fresh QR rendered to PNG — attach to chat; a new
                            line supersedes the previous image (codes rotate)
  QR_TEXT: <payload>        raw QR payload (qrcode lib unavailable) — caller
                            renders with any external tool
  NOTE: <text>              non-terminal degradation the user should hear
                            (e.g. this wacli build lacks --phone pairing)
  CONNECTED                 pairing succeeded AND the chats probe passed
  ERROR: <reason>           terminal failure (includes wacli's passkey-gated
                            stop, which it reports instead of rotating codes)

Prefer `--phone`: a QR shown in the user's chat cannot be scanned when the
chat is open on the same phone that must do the scanning. The pairing code
has no such trap.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time


def emit(line: str) -> None:
    print(line, flush=True)


def wacli_bin() -> str | None:
    return shutil.which("wacli")


def auth_status_ok(wacli: str) -> bool:
    try:
        out = subprocess.run([wacli, "auth", "status"], capture_output=True,
                             text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return False
    blob = (out.stdout + out.stderr).lower()
    return out.returncode == 0 and "not authenticated" not in blob


def chats_probe(wacli: str) -> bool:
    try:
        out = subprocess.run([wacli, "chats", "list", "--limit", "1"],
                             capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return out.returncode == 0


def auth_flag_surface(wacli: str) -> str:
    """`wacli auth --help` text — the flag surface varies widely by version
    (0.6.0 ships none of --events/--qr-format/--phone; 0.17.x ships all)."""
    try:
        out = subprocess.run([wacli, "auth", "--help"], capture_output=True,
                             text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return out.stdout + out.stderr


def wacli_version(wacli: str) -> str:
    try:
        out = subprocess.run([wacli, "--version"], capture_output=True,
                             text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return "unknown"
    return (out.stdout or out.stderr).strip() or "unknown"


# Must render under send_allowlist's /tmp/sutando- prefix (dir AND filename),
# else the [file:] marker is delivery-denied and the QR never reaches the chat.
_SENDABLE_QR_DIR = "/tmp"
_QR_FILE_PREFIX = "sutando-whatsapp-qr-"


def render_qr_png(payload: str, out_dir: str) -> str | None:
    """Render the raw payload to a PNG; None when the qrcode lib is absent."""
    try:
        import qrcode  # type: ignore
    except ImportError:
        return None
    img = qrcode.make(payload)
    fd, path = tempfile.mkstemp(prefix=_QR_FILE_PREFIX, suffix=".png", dir=out_dir)
    os.close(fd)
    img.save(path)
    return path


class EventRouter:
    """Route wacli `--events` NDJSON (and plain-text fallbacks) to protocol lines.

    Kept free of subprocess concerns so tests can drive it with synthetic
    streams — the production entrypoint feeds it the live wacli output.
    """

    # Fallback for wacli builds whose pairing code appears only in plain text.
    _CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")

    def __init__(self, qr_dir: str):
        self.qr_dir = qr_dir
        self.paired = False
        self.error: str | None = None

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line.startswith("{"):
            try:
                self._feed_event(json.loads(line))
                return
            except (json.JSONDecodeError, TypeError):
                pass  # JSON-looking prose: fall through to text handling
        self._feed_text(line)

    def _feed_event(self, ev: dict) -> None:
        kind = str(ev.get("type") or ev.get("event") or "").lower()
        # EventWriter nests material under `data`; flat shapes are fallbacks.
        # Only str payloads: stringifying the dict renders its repr as the QR.
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        # Exact matches first: "paired" must not fall into the "pair" branch.
        if kind in ("connected", "authenticated", "paired", "success"):
            self.paired = True
        elif "qr" in kind:
            payload = (data.get("code") or data.get("payload")
                       or ev.get("code") or ev.get("payload"))
            if isinstance(payload, str) and payload:
                self._emit_qr(payload)
        elif "pair" in kind:
            code = (data.get("code") or data.get("pairing_code")
                    or ev.get("code") or ev.get("pairing_code"))
            if isinstance(code, str) and code:
                emit(f"PAIR_CODE: {code}")
        elif kind in ("error", "fatal"):
            self.error = str(ev.get("message") or data.get("message")
                             or ev.get("error") or "unknown")

    def _feed_text(self, line: str) -> None:
        low = line.lower()
        if "passkey" in low:
            self.error = "passkey-gated pairing is not supported by wacli; " \
                         "link once via WhatsApp's QR flow on this Mac instead"
        elif "not authenticated" in low or "unauthenticated" in low:
            pass  # known negatives — must never read as a success signal
        elif "authenticated" in low or "pairing successful" in low:
            self.paired = True
        else:
            m = self._CODE_RE.search(line)
            if m and "pair" in low:
                emit(f"PAIR_CODE: {m.group(1)}")

    def _emit_qr(self, payload: str) -> None:
        path = render_qr_png(payload, self.qr_dir)
        if path:
            emit(f"QR_PNG: {path}")
        else:
            emit(f"QR_TEXT: {payload}")


def run_auth(wacli: str, phone: str | None, timeout_s: int, qr_dir: str) -> int:
    # Probe the flag surface: passing a flag this build lacks makes wacli exit
    # instantly on "unknown flag" and every guided run dies generically.
    flags = auth_flag_surface(wacli)
    if "--events" not in flags:
        # A flagless build renders its only QR as terminal block art — no
        # payload exists to relay, so pretending to pair would just time out.
        emit(f"ERROR: this wacli build ({wacli_version(wacli)}) lacks "
             "--events, so chat pairing cannot work — upgrade first: "
             "brew install openclaw/tap/wacli")
        return 1
    cmd = [wacli, "auth", "--events", "--qr-format", "text"]
    if phone:
        if "--phone" in flags:
            cmd += ["--phone", phone]
        else:
            emit("NOTE: this wacli build lacks --phone pairing; falling back "
                 "to the QR flow")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)
    router = EventRouter(qr_dir)

    def pump(stream):
        for raw in stream:
            router.feed(raw)

    threads = [threading.Thread(target=pump, args=(s,), daemon=True)
               for s in (proc.stdout, proc.stderr)]
    for t in threads:
        t.start()

    err_terminal = False
    timed_out = False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if router.error:
            err_terminal = True
            break
        if router.paired or proc.poll() is not None:
            break
        time.sleep(0.5)
    else:
        timed_out = True

    # Every exit reaps: wacli outlives `connected` (~30s bootstrap-sync idle
    # exit), and an error event must not abandon a live child either.
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if err_terminal:
        emit(f"ERROR: {router.error}")
        return 1
    # The session store is the authority on every non-error exit — including
    # timeout, where an unrecognised event vocabulary may hide a live pairing.
    if auth_status_ok(wacli) and chats_probe(wacli):
        emit("CONNECTED")
        return 0
    if timed_out:
        emit("ERROR: pairing timed out — ask the user to retry when ready")
    else:
        emit(f"ERROR: {router.error or 'auth ended without a valid session'}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phone", help="use WhatsApp phone-number pairing "
                                    "(preferred: no QR scan needed)")
    ap.add_argument("--timeout", type=int, default=180,
                    help="overall pairing bound in seconds (default 180)")
    ap.add_argument("--qr-dir", default=_SENDABLE_QR_DIR,
                    help="directory for rendered QR PNGs (default lands under "
                         "the send-allowlist /tmp/sutando- prefix so the QR is "
                         "deliverable through the chat bridges)")
    args = ap.parse_args()

    wacli = wacli_bin()
    if not wacli:
        emit("ERROR: wacli is not installed — run: "
             "brew install openclaw/tap/wacli")
        return 1

    if auth_status_ok(wacli) and chats_probe(wacli):
        emit("ALREADY_CONNECTED")
        return 0

    return run_auth(wacli, args.phone, args.timeout, args.qr_dir)


if __name__ == "__main__":
    sys.exit(main())
