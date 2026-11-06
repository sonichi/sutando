"""agent-api /task request guard — Content-Length cap + guest routing.

Exercises the real Handler.do_POST (not source text): an oversized or invalid
Content-Length is rejected BEFORE the body is read and BEFORE the guest handler
runs; a valid guest request within the cap is routed to the sandboxed worker; a
valid owner request within the cap is accepted and NOT routed to guest.

Run: `WORKSPACE_DIR=$(mktemp -d) python3 tests/agent_api_task_guard.test.py`.
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

# SUTANDO_TEST_MODE=1 makes resolve_workspace() honour $SUTANDO_WORKSPACE, keeping the
# owner path's task file in a tempdir. Must be set BEFORE agent-api.py is imported.
_TEST_WS = tempfile.mkdtemp()
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = _TEST_WS
SRC = Path(__file__).resolve().parent.parent / "src"
_spec = importlib.util.spec_from_file_location("agent_api", str(SRC / "agent-api.py"))
agent_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_api)

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("ok: " if cond else "FAIL: ") + name)
    if not cond:
        failures += 1


class FakeRFile:
    def __init__(self, body: bytes = b""):
        self.body = body
        self.read_calls = 0

    def read(self, n: int = -1) -> bytes:
        self.read_calls += 1
        return self.body


def make_handler(headers: dict, body: bytes = b"", auth: bool = True):
    h = agent_api.Handler.__new__(agent_api.Handler)
    h.path = "/task"
    h.headers = headers
    h.rfile = FakeRFile(body)
    h._responses = []
    h.check_auth = lambda: auth
    h.send_json = lambda status, data: h._responses.append((status, data))
    return h


def run() -> None:
    dispatched = []
    orig = agent_api.submit_signal_room_task

    def _spy(task, task_dir, confine, **kw):
        dispatched.append((task, kw))
        return orig(task, task_dir, confine, **kw)

    agent_api.submit_signal_room_task = _spy
    # Keep the owner path from touching anything heavy in the accept test.
    orig_emit = getattr(agent_api, "_emit_task_processed", None)
    agent_api._emit_task_processed = lambda *a, **k: None
    try:
        # 1. Oversized Content-Length -> 413, body NEVER read, guest NEVER invoked.
        h = make_handler({"Content-Length": "1000000"})
        h.do_POST()
        check("oversized request -> 413", bool(h._responses) and h._responses[0][0] == 413)
        check("oversized: body is never read into memory", h.rfile.read_calls == 0)
        check("oversized: nothing submitted", dispatched == [])

        # 2. Invalid (non-numeric) Content-Length -> 400, body never read.
        h = make_handler({"Content-Length": "not-a-number"})
        h.do_POST()
        check("invalid Content-Length -> 400", bool(h._responses) and h._responses[0][0] == 400)
        check("invalid: body never read", h.rfile.read_calls == 0)

        # 3. Valid guest request within the cap -> 200 + routed to the sandboxed worker.
        dispatched.clear()
        body = json.dumps({"from": "matrixrtc-voice-news", "task": "dig into item 2",
                           "access_tier": "guest"}).encode()
        h = make_handler({"Content-Length": str(len(body))}, body)
        h.do_POST()
        check("valid guest within cap -> 200", bool(h._responses) and h._responses[0][0] == 200)
        check("valid guest routed to the Signal Room submitter", len(dispatched) == 1)
        check("valid guest: body WAS read", h.rfile.read_calls >= 1)
        gtid = h._responses[0][1].get("task_id", "") if h._responses else ""
        check("guest task_id is a canonical task-signal-* id (Sutando executes it at team tier)",
              gtid.startswith("task-signal-"))

        # 4. Valid OWNER request within the cap -> accepted (200), NOT routed to guest.
        dispatched.clear()
        body = json.dumps({"from": "some-agent", "task": "owner question"}).encode()
        h = make_handler({"Content-Length": str(len(body))}, body)
        h.do_POST()
        check("valid owner within cap -> 200 (accepted)", bool(h._responses) and h._responses[0][0] == 200)
        check("valid owner NOT routed to the Signal Room submitter", dispatched == [])
        tid = h._responses[0][1].get("task_id", "") if h._responses else ""
        _ws = str(Path(_TEST_WS).resolve())  # resolve() so macOS /var -> /private/var matches
        check("owner task file lands ONLY in the temp workspace (never the live one)",
              bool(tid) and str(agent_api.TASK_DIR).startswith(_ws)
              and (agent_api.TASK_DIR / f"{tid}.txt").exists())

        # 5. Non-string task -> 400 (defends the owner from_agent/task line handling).
        dispatched.clear()
        body = json.dumps({"from": "x", "task": {"nested": "object"}}).encode()
        h = make_handler({"Content-Length": str(len(body))}, body)
        h.do_POST()
        check("non-string task -> 400", bool(h._responses) and h._responses[0][0] == 400)
    finally:
        agent_api.submit_signal_room_task = orig
        if orig_emit is not None:
            agent_api._emit_task_processed = orig_emit

    if failures:
        print(f"\n{failures} failure(s)")
        raise SystemExit(1)
    print("\nall ok")


run()
