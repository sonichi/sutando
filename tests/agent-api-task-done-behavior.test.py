#!/usr/bin/env python3
"""Behavioral coverage for the /task-done non-task- id guard (issue #1786).

Companion to agent-api-task-done-id-guard.test.py (structural): this one
actually executes the guard by spinning an in-process HTTP server and POSTing
/task-done with voice-*, proactive-*, and task-* ids. voice-*/proactive-*
must be accepted (200 — task-bridge legitimately posts them) but NOT stored
in task_history; task-* must be stored.

Run: python3 tests/agent-api-task-done-behavior.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.pop("SUTANDO_API_TOKEN", None)

spec = importlib.util.spec_from_file_location("agent_api", REPO / "src" / "agent-api.py")
api = importlib.util.module_from_spec(spec)
sys.modules["agent_api"] = api
spec.loader.exec_module(api)

tmp = Path(tempfile.mkdtemp(prefix="task-done-behavior-"))
api.TASK_DIR = tmp / "tasks"
api.RESULT_DIR = tmp / "results"
api.TASK_DIR.mkdir()
api.RESULT_DIR.mkdir()
api.API_TOKEN = ""
api.task_history.clear()

server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
server.timeout = 0.5
base = f"http://127.0.0.1:{server.server_address[1]}"

statuses: dict[str, int] = {}
done = threading.Event()


def worker() -> None:
    try:
        for tid in ("voice-1751234567", "proactive-1751234568", "task-real-1"):
            req = urllib.request.Request(
                f"{base}/task-done",
                method="POST",
                data=json.dumps({"taskId": tid, "result": "some result"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            statuses[tid] = urllib.request.urlopen(req, timeout=10).status
    except Exception as exc:  # pragma: no cover - failure path
        statuses["error"] = repr(exc)
    finally:
        done.set()


t = threading.Thread(target=worker)
t.start()
while not done.is_set():
    server.handle_request()
t.join()
server.server_close()

errors = 0


def check(cond: bool, msg: str) -> None:
    global errors
    if cond:
        print(f"ok: {msg}")
    else:
        errors += 1
        print(f"FAIL: {msg}", file=sys.stderr)


check("error" not in statuses, f"no request errors ({statuses.get('error')})")
check(statuses.get("voice-1751234567") == 200, "voice-* id accepted with 200 (bridge compatibility)")
check(statuses.get("proactive-1751234568") == 200, "proactive-* id accepted with 200")
check(statuses.get("task-real-1") == 200, "task-* id accepted with 200")
check("voice-1751234567" not in api.task_history, "voice-* id NOT stored in task_history")
check("proactive-1751234568" not in api.task_history, "proactive-* id NOT stored in task_history")
check("task-real-1" in api.task_history, "task-* id stored in task_history")

if errors:
    print(f"FAILED: {errors} check(s) failed", file=sys.stderr)
    sys.exit(1)
print("PASSED: /task-done id guard behaves correctly")
