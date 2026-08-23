#!/usr/bin/env python3
"""The PRODUCTION gateway process must honor the destined-proactive gate.

The loader defines `PROACTIVE_CLAIM_GATE` below its `exec` of the canonical
source — whose own `if __name__ == "__main__": main()` guard fired inside
that exec when launched as a script, so main() never returned and nothing
below the exec ran: the gate stayed None in production while every importer
(all prior tests) saw it installed. Observed live 2026-08-22: the gateway
claimed and delivered `proactive-*.to-discord.txt` files to its own room.

So this suite runs the REAL entry point — `python3 src/remote-gateway-bridge.py`
as a subprocess, argv-identical to production — against a mock gateway:
an undestined proactive file must be delivered (positive control: the
pipeline is live) while a discord-destined one must never be posted and
must remain untouched on disk.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


STATE = {"room_posts": [], "polls": 0}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/tasks"):
            with LOCK:
                STATE["polls"] += 1
            time.sleep(1)
            self._json({"tasks": []})
            return
        self._json({"ok": True})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln) if ln else b"{}"
        if self.path.startswith("/v1/room"):
            try:
                with LOCK:
                    STATE["room_posts"].append(json.loads(raw))
            except ValueError:
                pass
        self._json({"ok": True})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp(prefix="destined-gate-")
rdir = Path(tmp) / "results"
rdir.mkdir(parents=True)
sdir = Path(tmp) / "state"
sdir.mkdir(parents=True)
# Recent ag2space owner activity: routing legitimately prefers the gateway
# for UNDESTINED files, so the positive control lands without the 180s grace.
(sdir / "last-owner-activity.json").write_text(json.dumps(
    {"ts": int(time.time()), "channel": "ag2space", "summary": "t"}))
# poison sorts FIRST: a partial write mid-character must not block the drain
(rdir / "proactive-000.txt").write_bytes(b"\x80\x81 truncated multibyte")
(rdir / "proactive-100.txt").write_text("undestined control body")
(rdir / "proactive-101.to-discord.txt").write_text("discord destined body")

env = dict(os.environ)
env.update({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": tmp,
            "REMOTE_TASK_URL": f"http://127.0.0.1:{port}",
            "REMOTE_TASK_TOKEN": "testtoken",
            "REMOTE_TASK_PROVIDER": "remote-gateway",
            "REMOTE_TASK_POLL_WAIT": "1",
            "REMOTE_OUTBOUND_SCAN_S": "1",
            "REMOTE_PROACTIVE_ROOM": "!mock:example.org"})
env.pop("GATEWAY_INSTANCE", None)

proc = subprocess.Popen(
    [sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
    cwd=str(REPO), env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

try:
    deadline = time.monotonic() + 25
    delivered_control = False
    while time.monotonic() < deadline:
        with LOCK:
            bodies = [p.get("body", "") for p in STATE["room_posts"]]
        if any("undestined control body" in b for b in bodies):
            delivered_control = True
            break
        if proc.poll() is not None:
            break
    check(proc.poll() is None, "bridge process is alive (main() actually ran)")
    check(delivered_control,
          "positive control: undestined proactive IS delivered to /v1/room")

    # Give the drain several more cycles to (wrongly) take the destined file.
    time.sleep(4)
    with LOCK:
        bodies = [p.get("body", "") for p in STATE["room_posts"]]
    check(not any("discord destined body" in b for b in bodies),
          "destined .to-discord body never reaches the gateway's room")
    check((rdir / "proactive-101.to-discord.txt").exists(),
          "destined file remains on disk under its original name")
    check((rdir / "proactive-000.txt").exists(),
          "undecodable (mid-write) file is skipped, not consumed")
finally:
    proc.kill()
    out = proc.stdout.read().decode(errors="replace")[-1500:]

# ── Phase 2: activity routes to DISCORD; only the BODY names a channel —
# matrix-targeted must be claimed promptly, discord-targeted never. ──
tmp2 = tempfile.mkdtemp(prefix="destined-gate2-")
rdir2 = Path(tmp2) / "results"
rdir2.mkdir(parents=True)
sdir2 = Path(tmp2) / "state"
sdir2.mkdir(parents=True)
(sdir2 / "last-owner-activity.json").write_text(json.dumps(
    {"ts": int(time.time()), "channel": "discord", "summary": "t"}))
(rdir2 / "proactive-200.txt").write_text(
    "[channel: !legacy:ag2.space]\nmatrix targeted body")
(rdir2 / "proactive-201.txt").write_text(
    "[channel: 123456789012345678]\ndiscord targeted body")
env2 = dict(env)
env2["SUTANDO_WORKSPACE"] = tmp2
proc2 = subprocess.Popen(
    [sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
    cwd=str(REPO), env=env2,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    deadline = time.monotonic() + 25
    matrix_delivered = False
    while time.monotonic() < deadline:
        with LOCK:
            bodies = [p.get("body", "") for p in STATE["room_posts"]]
        if any("matrix targeted body" in b for b in bodies):
            matrix_delivered = True
            break
        if proc2.poll() is not None:
            break
    check(matrix_delivered,
          "reverse direction: matrix-targeted BODY is claimed despite "
          "discord activity (well inside the 180s grace)")
    time.sleep(4)
    with LOCK:
        bodies = [p.get("body", "") for p in STATE["room_posts"]]
    check(not any("discord targeted body" in b for b in bodies),
          "discord-targeted body never reaches the gateway room")
    check((rdir2 / "proactive-201.txt").exists(),
          "discord-targeted file remains on disk for its own bridge")
finally:
    proc2.kill()
    out2 = proc2.stdout.read().decode(errors="replace")[-1500:]
    out = out + "\n--- phase2 ---\n" + out2

if failures:
    print("--- bridge output tail ---")
    print(out)
print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
