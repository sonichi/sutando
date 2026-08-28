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
finally:
    proc.kill()
    out = proc.stdout.read().decode(errors="replace")[-1500:]

if failures:
    print("--- bridge output tail ---")
    print(out)
print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
