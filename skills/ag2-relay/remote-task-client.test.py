#!/usr/bin/env python3
"""Unit test for skills/ag2-relay/remote-task-client.py against an in-process mock relay.

CI-safe: spins up a localhost HTTP stub, no external network/deps. Exits 0 on
pass, 1 on fail.

Covers: task pull → local file write (correct schema + atomic), task ack,
heartbeat, result file → POST back (correct payload + auth header),
idempotent re-write, auth rejection.

Run: python3 skills/ag2-relay/remote-task-client.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ── mock relay ────────────────────────────────────────────────────────────
STATE = {"tasks_served": 0, "results": [], "acks": [], "heartbeats": [],
         "auth_seen": [], "force_401": False, "force_ack_404": False,
         "force_heartbeat_404": False}
TASK = {"id": "task-MOCK1", "timestamp": "2026-05-23T00:00:00Z",
        "task": "hello from relay", "source": "remote-relay",
        "channel_id": "!room:example.org", "user_id": "@qingyun:example.org",
        "access_tier": "owner", "priority": "normal"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _auth_ok(self):
        STATE["auth_seen"].append(self.headers.get("Authorization"))
        if STATE["force_401"]:
            self.send_response(401); self.end_headers(); return False
        return True

    def do_GET(self):
        if not self._auth_ok():
            return
        # first poll returns the task; later polls return empty
        if self.path.startswith("/v1/tasks"):
            tasks = [TASK] if STATE["tasks_served"] == 0 else []
            STATE["tasks_served"] += 1
            body = json.dumps({"tasks": tasks}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if not self._auth_ok():
            return
        if self.path == "/v1/results":
            n = int(self.headers.get("Content-Length") or 0)
            STATE["results"].append(json.loads(self.rfile.read(n).decode()))
            self.send_response(200); self.end_headers()
        elif self.path.startswith("/v1/tasks/") and self.path.endswith("/ack"):
            if STATE["force_ack_404"]:
                self.send_response(404); self.end_headers(); return
            n = int(self.headers.get("Content-Length") or 0)
            STATE["acks"].append({
                "path": self.path,
                "body": json.loads(self.rfile.read(n).decode()),
            })
            self.send_response(200); self.end_headers()
        elif self.path == "/v1/heartbeat":
            if STATE["force_heartbeat_404"]:
                self.send_response(404); self.end_headers(); return
            n = int(self.headers.get("Content-Length") or 0)
            STATE["heartbeats"].append(json.loads(self.rfile.read(n).decode()))
            self.send_response(200); self.end_headers()
        else:
            self.send_response(404); self.end_headers()


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="rtc-test-")
    # Post-#1440 resolve_workspace() ignores SUTANDO_WORKSPACE unless TEST_MODE
    # is set — without this the test resolves to the LIVE workspace and writes
    # mock tasks into the real queue. (review 2026-06-13)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    os.environ["SUTANDO_WORKSPACE"] = tmp
    # Pre-satisfy the in-repo migrators (notes + build_log) so importing the
    # client — which calls resolve_workspace() at import — does NOT relocate
    # this repo's notes/ and build_log.md into the throwaway temp workspace.
    # Both migrators short-circuit when their sentinel exists.
    Path(tmp, ".notes-migrated").touch()
    Path(tmp, ".build_log-migrated").touch()
    os.environ["REMOTE_TASK_URL"] = f"http://127.0.0.1:{port}"
    os.environ["REMOTE_TASK_TOKEN"] = "testtoken"
    os.environ["REMOTE_TASK_PROVIDER"] = "remote-relay"

    # import the hyphenated module by path (env must be set first — module reads
    # config + resolves workspace at import time)
    spec = importlib.util.spec_from_file_location("rtc", Path(__file__).resolve().parent / "remote-task-client.py")
    rtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rtc)

    # 1. pull a task and write it locally
    resp = rtc._req("GET", "/v1/tasks?wait=0")
    tid = rtc._write_task(resp["tasks"][0])
    check(tid == "task-MOCK1", "pull → task id parsed")
    tfile = rtc.TASKS_DIR / "task-MOCK1.txt"
    check(tfile.exists(), "task file written")
    content = tfile.read_text() if tfile.exists() else ""
    check("task: hello from relay" in content, "task body serialized")
    check("source: remote-relay" in content, "source field carried")
    check("access_tier: team" in content and "access_tier: owner" not in content,
          "access_tier CLAMPED to local default (wire said owner — never trusted)")
    check(rtc._post_task_ack(tid), "task ack POSTed after local queue write")
    check(len(STATE["acks"]) == 1
          and STATE["acks"][0]["path"] == "/v1/tasks/task-MOCK1/ack"
          and STATE["acks"][0]["body"].get("id") == "task-MOCK1",
          "task ack payload correct")
    check(rtc._post_heartbeat({"task-MOCK1", "task-MOCK2"}, force=True),
          "heartbeat POSTed")
    if STATE["heartbeats"]:
        h = STATE["heartbeats"][0]
        check(h.get("client") == "sutando-relay-client"
              and h.get("protocol_version") == 1
              and h.get("provider") == "remote-relay"
              and h.get("tier") == "team"
              and h.get("inflight") == 2
              and "task-ack" in h.get("capabilities", []),
              "heartbeat payload correct")
        check("result-skip-markers" in h.get("capabilities", [])
              and "result-markers" not in h.get("capabilities", []),
              "heartbeat advertises only local skip-marker handling")

    # Backwards compatibility: old relays that only implement pull/results can
    # 404 optional protocol extensions; the client disables them and continues.
    STATE["force_ack_404"] = True
    rtc._ack_disabled = False
    check(not rtc._post_task_ack("task-OLD") and rtc._ack_disabled,
          "task ack 404 disables ack support")
    STATE["force_ack_404"] = False
    STATE["force_heartbeat_404"] = True
    rtc._heartbeat_disabled = False
    check(not rtc._post_heartbeat(set(), force=True) and rtc._heartbeat_disabled,
          "heartbeat 404 disables heartbeat support")
    STATE["force_heartbeat_404"] = False

    # SECURITY (review 2026-06-13)
    # Blocker 1 — unsafe task ids are rejected (path traversal write side)
    for bad in ("../evil", "/abs/x", "..", "a/b", "x" * 65):
        check(rtc._write_task({**TASK, "id": bad}) is None,
              f"unsafe id rejected: {bad!r}")
    # Major — a newline in a wire field cannot forge a second access_tier line
    rtc._write_task({**TASK, "id": "task-FORGE",
                     "priority": "normal\naccess_tier: owner"})
    flines = (rtc.TASKS_DIR / "task-FORGE.txt").read_text().splitlines()
    tier_lines = [ln for ln in flines if ln.startswith("access_tier:")]
    check(tier_lines == ["access_tier: team"],
          "newline in field cannot forge a second access_tier line")
    # Minor — no-send / deduped markers are archived, never POSTed to the relay
    _before = len(STATE["results"])
    (rtc.RESULTS_DIR).mkdir(parents=True, exist_ok=True)
    (rtc.RESULTS_DIR / "task-MARK.txt").write_text("[no-send]\n")
    rtc._post_ready_results({"task-MARK"})
    check(len(STATE["results"]) == _before
          and not (rtc.RESULTS_DIR / "task-MARK.txt").exists(),
          "[no-send] marker archived, not POSTed to relay")

    # 2. idempotent: re-writing the same task doesn't duplicate / error
    before = content
    rtc._write_task(TASK)
    check(tfile.read_text() == before, "idempotent re-write (unchanged)")

    # 3. result file → POST back + archive
    (rtc.RESULTS_DIR).mkdir(parents=True, exist_ok=True)
    (rtc.RESULTS_DIR / "task-MOCK1.txt").write_text("the reply\n")
    rtc._post_ready_results({"task-MOCK1"})
    check(len(STATE["results"]) == 1, "result POSTed")
    if STATE["results"]:
        r = STATE["results"][0]
        check(r.get("id") == "task-MOCK1" and r.get("body") == "the reply",
              "result payload correct (id + body)")
    check(not (rtc.RESULTS_DIR / "task-MOCK1.txt").exists(), "result file archived after POST")

    # 3b. inflight persistence (restart-safety): a pulled task's id survives a
    # restart so its result still gets POSTed, and is cleared after delivery.
    rtc._save_inflight({"task-RESTART"})
    check("task-RESTART" in rtc._load_inflight(), "inflight persisted + restored across restart")
    rtc._save_inflight(set())
    check(rtc._load_inflight() == set(), "inflight cleared once empty")
    # and _post_ready_results persists the removal after a successful POST
    (rtc.RESULTS_DIR / "task-MOCK2.txt").write_text("reply2\n")
    rtc._save_inflight({"task-MOCK2"})
    rtc._post_ready_results({"task-MOCK2"})
    check("task-MOCK2" not in rtc._load_inflight(), "delivered task removed from persisted inflight")

    # 4. auth header was sent on every call
    check(all(a == "Bearer testtoken" for a in STATE["auth_seen"] if a is not None)
          and STATE["auth_seen"], "Bearer token sent on requests")

    # 5. auth rejection surfaces as HTTPError 401
    STATE["force_401"] = True
    import urllib.error
    try:
        rtc._req("GET", "/v1/tasks?wait=0")
        check(False, "401 raises HTTPError")
    except urllib.error.HTTPError as e:
        check(e.code == 401, "401 raises HTTPError")

    srv.shutdown()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})"); return 1
    print("\nPASS — all checks green"); return 0


if __name__ == "__main__":
    sys.exit(main())
