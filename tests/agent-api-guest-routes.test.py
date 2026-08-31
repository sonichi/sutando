"""agent-api guest routes — /guest-task, /capabilities, and /task tier handling.

Exercises the real Handler.do_POST / do_GET (not source text), mirroring
tests/agent_api_task_guard.test.py's harness:

  * POST /guest-task derives guest authority from the ROUTE — an untrusted body
    cannot elect its own privilege — and never writes an owner task;
  * its Content-Length guard rejects oversized/malformed headers BEFORE the body
    is read, exactly like the owner lane;
  * POST /task accepts the demote-only "guest" compat shim (shipped daemons still
    post there) and REFUSES any other explicit tier rather than falling through to
    owner authority;
  * GET /capabilities reports the guest_deep_dive readiness signal, is auth-gated,
    and degrades to available:false with a reason when the lane raises.

Run: `python3 tests/agent-api-guest-routes.test.py`
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

# Must be set BEFORE agent-api.py is imported: keeps any owner-path task file in a
# tempdir instead of the live workspace.
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


def make_handler(path: str, headers: dict, body: bytes = b"", auth: bool = True):
    h = agent_api.Handler.__new__(agent_api.Handler)
    h.path = path
    h.headers = headers
    h.rfile = FakeRFile(body)
    h._responses = []
    h.check_auth = lambda: (auth if auth else h._responses.append((401, {"error": "unauthorized"})) or False)
    h.send_json = lambda status, data: h._responses.append((status, data))
    h.send_private_json = lambda status, data: h._responses.append((status, data))
    return h


def post(path: str, payload: dict, auth: bool = True):
    body = json.dumps(payload).encode()
    h = make_handler(path, {"Content-Length": str(len(body))}, body, auth=auth)
    h.do_POST()
    return h


def run() -> None:
    dispatched = []
    orig_guest = agent_api.start_guest_deep_dive
    agent_api.start_guest_deep_dive = lambda *a, **k: dispatched.append(a)
    orig_emit = getattr(agent_api, "_emit_task_processed", None)
    agent_api._emit_task_processed = lambda *a, **k: None
    try:
        # --- POST /guest-task: the route decides the tier -----------------------
        dispatched.clear()
        h = post("/guest-task", {"task": "what happened with item 2?"})
        check("/guest-task -> 200", bool(h._responses) and h._responses[0][0] == 200)
        check("/guest-task routes to the sandboxed worker", len(dispatched) == 1)
        tid = h._responses[0][1].get("task_id", "") if h._responses else ""
        check("/guest-task id is namespaced signal-guest-", tid.startswith("signal-guest-"))
        check("/guest-task result_url points at the async contract",
              (h._responses[0][1].get("result_url") or "") == f"/result/{tid}")

        # A body cannot escalate itself on the guest route.
        dispatched.clear()
        h = post("/guest-task", {"task": "escalate me", "access_tier": "owner"})
        gid = h._responses[0][1].get("task_id", "") if h._responses else ""
        check("/guest-task ignores a body-declared owner tier (route wins)",
              h._responses[0][0] == 200 and gid.startswith("signal-guest-") and len(dispatched) == 1)

        # Missing/blank task -> 400, worker never invoked.
        dispatched.clear()
        h = post("/guest-task", {"task": "   "})
        check("/guest-task blank task -> 400", h._responses[0][0] == 400)
        check("/guest-task blank task never reaches the worker", dispatched == [])

        # Size guard runs BEFORE the body is read (same as the owner lane).
        dispatched.clear()
        h = make_handler("/guest-task", {"Content-Length": "1000000"})
        h.do_POST()
        check("/guest-task oversized -> 413", h._responses[0][0] == 413)
        check("/guest-task oversized: body never read", h.rfile.read_calls == 0)
        check("/guest-task oversized: worker never invoked", dispatched == [])

        h = make_handler("/guest-task", {"Content-Length": "not-a-number"})
        h.do_POST()
        check("/guest-task invalid Content-Length -> 400", h._responses[0][0] == 400)
        check("/guest-task invalid: body never read", h.rfile.read_calls == 0)

        # Malformed JSON within the cap -> 400.
        h = make_handler("/guest-task", {"Content-Length": "5"}, b"{not")
        h.do_POST()
        check("/guest-task malformed JSON -> 400", h._responses[0][0] == 400)

        # Auth gate.
        dispatched.clear()
        h = post("/guest-task", {"task": "x"}, auth=False)
        check("/guest-task honours check_auth", dispatched == [])

        # --- POST /task: demote-only shim, never escalation ---------------------
        dispatched.clear()
        h = post("/task", {"from": "matrixrtc-voice-news", "task": "shipped daemon",
                           "access_tier": "guest"})
        stid = h._responses[0][1].get("task_id", "") if h._responses else ""
        check("/task with access_tier guest still routes to the guest lane (compat shim)",
              h._responses[0][0] == 200 and len(dispatched) == 1 and stid.startswith("signal-guest-"))

        for tier in ("owner", "team", "admin", "", "GUEST"):
            dispatched.clear()
            h = post("/task", {"from": "x", "task": "t", "access_tier": tier})
            check(f"/task refuses access_tier={tier!r} (no silent owner fallthrough)",
                  h._responses[0][0] == 400 and dispatched == [])

        # --- GET /capabilities ---------------------------------------------------
        def get(path: str, auth: bool = True):
            h = make_handler(path, {}, auth=auth)
            h.do_GET()
            return h

        orig_avail = None
        import signal_guest_handler as sgh
        orig_avail = sgh.guest_availability

        sgh.guest_availability = lambda *a, **k: (True, None)
        h = get("/capabilities")
        body = h._responses[0][1] if h._responses else {}
        check("/capabilities -> 200", bool(h._responses) and h._responses[0][0] == 200)
        check("/capabilities reports available:true when the lane is ready",
              body.get("guest_deep_dive", {}).get("available") is True)
        check("/capabilities omits reason when ready",
              "reason" not in body.get("guest_deep_dive", {}))

        sgh.guest_availability = lambda *a, **k: (False, "worker_missing")
        h = get("/capabilities")
        cap = h._responses[0][1].get("guest_deep_dive", {}) if h._responses else {}
        check("/capabilities reports available:false with the machine-readable reason",
              cap.get("available") is False and cap.get("reason") == "worker_missing")

        def boom(*a, **k):
            raise RuntimeError("lane exploded")

        sgh.guest_availability = boom
        h = get("/capabilities")
        cap = h._responses[0][1].get("guest_deep_dive", {}) if h._responses else {}
        check("/capabilities fails closed when the lane raises",
              h._responses[0][0] == 200 and cap.get("available") is False
              and str(cap.get("reason", "")).startswith("capability_error"))

        h = get("/capabilities", auth=False)
        check("/capabilities honours check_auth",
              not any(r[0] == 200 for r in h._responses))

        sgh.guest_availability = orig_avail

        # --- the decisive property: no owner task from any guest submission ------
        owner_files = list(Path(agent_api.TASK_DIR).glob("*")) if Path(agent_api.TASK_DIR).exists() else []
        check("no owner task file was written by any guest-route submission",
              all(not f.name.startswith("task-") for f in owner_files))
    finally:
        agent_api.start_guest_deep_dive = orig_guest
        if orig_emit is not None:
            agent_api._emit_task_processed = orig_emit

    print()
    if failures:
        print(f"FAILED ({failures})")
        sys.exit(1)
    print("all ok")


run()
