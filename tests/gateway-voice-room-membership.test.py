#!/usr/bin/env python3
"""The PRODUCTION gateway process enforces room membership for voice rooms.

Runs the real entry point — `python3 src/remote-gateway-bridge.py` — against
a mock gateway whose `/v1/room {"op": "members"}` knows three rooms: one where
the agent and its owner are both joined, one the agent cannot read (a forged
but well-formed id), and one the owner has never joined.

Negative integration: a `proactive-result-*` file addressed to the forged
room, or to the owner-less room, is never posted and stays on disk; the
positive control addressed to the verified room is delivered there. The
verdict files the task bridge would read say the same: the forged room is
refused, the verified room is not.
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
sys.path.insert(0, str(REPO / "src"))
from voice_room_membership import CHECK_DIR_NAME, REQUEST_SUFFIX, VERDICT_SUFFIX  # noqa: E402

AGENT = "@mock-agent:example.org"
OWNER = "@o:example.org"
OK_ROOM = "!ok:example.org"
FORGED_ROOM = "!forged:example.org"
NO_OWNER_ROOM = "!noowner:example.org"

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


STATE = {"room_posts": [], "member_reads": []}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/agents"):
            self._json({"agents": [{"id": AGENT, "owner": OWNER, "owner_dm_room": "!mock:example.org"}]})
            return
        if self.path.startswith("/v1/tasks"):
            time.sleep(1)
            self._json({"tasks": []})
            return
        self._json({"ok": True})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln) if ln else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        if self.path.startswith("/v1/room") and payload.get("op") == "members":
            room = payload.get("room_id")
            with LOCK:
                STATE["member_reads"].append(room)
            if room == OK_ROOM:
                self._json({"members": [{"user_id": AGENT, "display_name": "A"}, {"user_id": OWNER}]})
            elif room == NO_OWNER_ROOM:
                self._json({"members": [{"user_id": AGENT}, {"user_id": "@stranger:example.org"}]})
            else:
                self._json({"error": "members read failed (HTTP 403)"})
            return
        if self.path.startswith("/v1/room"):
            with LOCK:
                STATE["room_posts"].append(payload)
        self._json({"ok": True, "event_id": "$mock"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

tmp = tempfile.mkdtemp(prefix="voice-room-gate-")
rdir = Path(tmp) / "results"
rdir.mkdir(parents=True)
sdir = Path(tmp) / "state"
sdir.mkdir(parents=True)
cdir = sdir / CHECK_DIR_NAME
cdir.mkdir(parents=True)
# Owner last seen on Discord: only the `.to-ag2space` name tag brings these
# files to this gateway (an untagged name would wait on the Discord bridge).
(sdir / "last-owner-activity.json").write_text(json.dumps(
    {"ts": int(time.time()), "channel": "discord", "summary": "t"}))
# The task bridge's shape for a room-bound voice result (forwardVoiceResultToRoom).
forged = rdir / "proactive-result-task-1700000000001-1800000001.to-ag2space.txt"
forged.write_text(f"[channel: {FORGED_ROOM}]\nforged room body")
no_owner = rdir / "proactive-result-task-1700000000002-1800000002.to-ag2space.txt"
no_owner.write_text(f"[channel: {NO_OWNER_ROOM}]\nowner-less room body")
ok = rdir / "proactive-result-task-1700000000003-1800000003.to-ag2space.txt"
ok.write_text(f"[channel: {OK_ROOM}]\nverified room body")
# The task bridge's binding questions, as it writes them.
(cdir / f"k-forged{REQUEST_SUFFIX}").write_text(json.dumps({"room_id": FORGED_ROOM, "requested_at": time.time()}))
(cdir / f"k-ok{REQUEST_SUFFIX}").write_text(json.dumps({"room_id": OK_ROOM, "requested_at": time.time()}))

env = dict(os.environ)
env.update({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": tmp,
            "REMOTE_TASK_URL": f"http://127.0.0.1:{port}",
            "REMOTE_TASK_TOKEN": "testtoken",
            "REMOTE_TASK_PROVIDER": "remote-gateway",
            "REMOTE_TASK_POLL_WAIT": "1",
            "REMOTE_OUTBOUND_SCAN_S": "1",
            "REMOTE_PROACTIVE_ROOM": "!mock:example.org",
            "AGENT_MXID": AGENT})
env.pop("GATEWAY_INSTANCE", None)

proc = subprocess.Popen(
    [sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
    cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
out = ""
try:
    deadline = time.monotonic() + 25
    delivered_ok = False
    while time.monotonic() < deadline and proc.poll() is None:
        with LOCK:
            posts = list(STATE["room_posts"])
        if any(p.get("room_id") == OK_ROOM and "verified room body" in p.get("body", "") for p in posts):
            delivered_ok = True
            break
        time.sleep(0.05)
    check(proc.poll() is None, "bridge process is alive (main() actually ran)")
    check(delivered_ok, "positive control: the result addressed to the verified room IS posted there")

    # Several more drain passes for the refused files to be (wrongly) taken.
    time.sleep(4)
    with LOCK:
        posts = list(STATE["room_posts"])
        reads = list(STATE["member_reads"])
    rooms_posted = {p.get("room_id") for p in posts}
    bodies = [p.get("body", "") for p in posts]
    check(FORGED_ROOM not in rooms_posted and not any("forged room body" in b for b in bodies),
          "forged room: nothing is ever posted there")
    check(NO_OWNER_ROOM not in rooms_posted and not any("owner-less room body" in b for b in bodies),
          "owner-less room: nothing is ever posted there (agent alone is not enough)")
    check(not any("forged room body" in b or "owner-less room body" in b for b in bodies),
          "refused bodies never reach ANY room, the owner DM included")
    check(forged.exists() and no_owner.exists(),
          "refused files stay on disk under their original names (held, not eaten)")
    check(not ok.exists(), "the verified file was claimed and archived")
    check(FORGED_ROOM in reads and OK_ROOM in reads and NO_OWNER_ROOM in reads,
          f"membership was asked of the gateway for every room ({reads})")
    check(reads.count(FORGED_ROOM) == 1 and reads.count(OK_ROOM) == 1,
          f"one gateway read per room across the request and the gate (cached): {reads}")

    vf = cdir / f"k-forged{VERDICT_SUFFIX}"
    vo = cdir / f"k-ok{VERDICT_SUFFIX}"
    check(vf.exists() and vo.exists(), "both binding requests were answered with a verdict file")
    if vf.exists() and vo.exists():
        f_verdict = json.loads(vf.read_text())
        o_verdict = json.loads(vo.read_text())
        check(f_verdict["room_id"] == FORGED_ROOM and f_verdict["verified"] is False
              and f_verdict["reason"] == "members unreadable",
              f"forged room verdict is a refusal: {f_verdict}")
        check(o_verdict["room_id"] == OK_ROOM and o_verdict["verified"] is True
              and o_verdict["agent_joined"] and o_verdict["owner_joined"],
              f"verified room verdict passes: {o_verdict}")
    check(not (cdir / f"k-forged{REQUEST_SUFFIX}").exists() and not (cdir / f"k-ok{REQUEST_SUFFIX}").exists(),
          "answered requests are removed")
finally:
    if proc.poll() is None:
        proc.kill()
    out = proc.communicate(timeout=10)[0].decode(errors="replace")
    srv.shutdown()
    srv.server_close()

check("voice-room: holding proactive-result-task-1700000000001-1800000001.to-ag2space.txt" in out
      and out.count("voice-room: holding proactive-result-task-1700000000001") == 1,
      "the held forged file is logged once, not per pass")
check(f"voice-room: {FORGED_ROOM} REFUSED (members unreadable)" in out
      and f"voice-room: {OK_ROOM} verified (agent and owner joined)" in out,
      "the verifier logs each answered request")

if failures:
    print("--- bridge output tail ---")
    print(out[-3000:])
print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.stdout.flush()
sys.stderr.flush()
# Flush coverage before the hard exit (os._exit skips coverage's atexit writer).
try:
    import coverage
    _cov = coverage.Coverage.current()
    if _cov is not None:
        _cov.save()
except Exception:
    pass
os._exit(1 if failures else 0)
