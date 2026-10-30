#!/usr/bin/env python3
"""A poll-stall exit must not cut off a send the relay has already accepted.

Runs the REAL bridge entry point twice against a loopback gateway. The relay
accepts the owner's proactive message at once but holds its response longer
than a short join would wait, while every task poll fails so the stall exit
trips mid-send. Exiting then would strand the `.sending.<pid>` claim, and the
next process's orphan recovery would post the same message again.

Pinned: the first process exits on the stall code only after the send settled,
the message is archived, and the restarted process posts nothing: one POST.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
BODY = "stall-exit in-flight proactive body"
SCAN_S = 0.2
STALL_S = 2
# Longer than the old join budget (scan * 3 + 5) after the stall trips, and
# shorter than the proactive POST timeout, so the relay's answer is a success.
RESPONSE_DELAY_S = 10.0

STATE = {"posts": 0}
LOCK = threading.Lock()
failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/agents"):
            self._json({"agents": [{"id": "@mock-agent:example.org", "owner": "@o:example.org",
                                    "owner_dm_room": "!mock:example.org"}]})
        elif self.path.startswith("/v1/tasks"):
            self._json({"error": "relay down"}, code=503)  # every poll fails: a stall
        else:
            self._json({"ok": True})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(ln) if ln else b"{}")
        except ValueError:
            payload = {}
        if self.path.startswith("/v1/room") and payload.get("op") == "members":
            # The bridge adopts the owner DM only when it holds just the agent and its owner.
            self._json({"members": [{"user_id": "@mock-agent:example.org"}, {"user_id": "@o:example.org"}]})
            return
        if self.path.startswith("/v1/room") and payload.get("body") == BODY:
            with LOCK:
                STATE["posts"] += 1
                first = STATE["posts"] == 1
            if first:
                time.sleep(RESPONSE_DELAY_S)  # accepted; the answer is merely slow
        self._json({"ok": True, "event_id": "$mock"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()

tmp = Path(tempfile.mkdtemp(prefix="stall-inflight-"))
rdir = tmp / "results"
sdir = tmp / "state"
rdir.mkdir(parents=True)
sdir.mkdir(parents=True)
(sdir / "last-owner-activity.json").write_text(json.dumps(
    {"ts": int(time.time()), "channel": "ag2space", "summary": "t"}))
(rdir / "proactive-200.txt").write_text(BODY)

env = hermetic_env()
env.update({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(tmp),
            "REMOTE_TASK_URL": f"http://127.0.0.1:{srv.server_address[1]}",
            "REMOTE_TASK_TOKEN": "testtoken",
            "REMOTE_TASK_PROVIDER": "remote-gateway",
            "REMOTE_TASK_POLL_WAIT": "1",
            "REMOTE_OUTBOUND_SCAN_S": str(SCAN_S),
            "REMOTE_OUTBOUND_WATCHER": "off",
            "REMOTE_TASK_POLL_STALL_EXIT_S": str(STALL_S),
            "SUTANDO_BRIDGE_RESTART_OWNER": "launchd",
            "REMOTE_PROACTIVE_ROOM": "!mock:example.org",
            "AGENT_MXID": "@mock-agent:example.org"})
env.pop("GATEWAY_INSTANCE", None)


def run_bridge(timeout: float):
    proc = subprocess.Popen([sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
                            cwd=str(REPO), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out = proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        out = proc.communicate(timeout=10)[0]
    return proc, out.decode(errors="replace")


def expire_lock():
    # A bridge killed without releasing its lease would make the next start stand down.
    lock = sdir / "locks" / "gateway-bridge.lock"
    if lock.exists():
        lease = json.loads(lock.read_text(encoding="utf-8"))
        lease["heartbeat_at"] = 0
        lock.write_text(json.dumps(lease), encoding="utf-8")


outs = []
try:
    start = time.monotonic()
    first, out = run_bridge(timeout=90)
    elapsed = time.monotonic() - start
    outs.append(out)
    claims = sorted(p.name for p in rdir.glob("proactive-200.sending.*"))
    archived = sorted(p.name for p in (rdir / "archive").rglob("proactive-200*")) \
        if (rdir / "archive").exists() else []
    check(first.returncode == 69, f"first bridge exits on the stall code (rc={first.returncode})")
    check(elapsed >= RESPONSE_DELAY_S, f"the exit waited for the slow answer ({elapsed:.1f}s)")
    check(not claims, f"no claim stranded under the dead pid ({claims})")
    check(bool(archived), "the accepted message was archived as delivered")

    expire_lock()
    second, out = run_bridge(timeout=60)
    outs.append(out)
    with LOCK:
        posts = STATE["posts"]
    check(second.returncode == 69, f"restarted bridge runs and stalls out (rc={second.returncode})")
    check(posts == 1, f"the relay accepted the message exactly once (posts={posts})")
finally:
    srv.shutdown()
    srv.server_close()

if failures:
    for i, o in enumerate(outs, 1):
        print(f"--- bridge run {i} output tail ---")
        print(o[-2000:])
    print(f"\nFAILED — {len(failures)} failure(s)")
    sys.exit(1)
print("\nOK — 0 failure(s)")
