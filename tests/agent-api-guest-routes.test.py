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
    # The route's job is to SUBMIT a Signal Room task; Sutando executes it. We spy on
    # the submitter, never on an engine — there is no engine at this layer any more.
    dispatched = []
    orig_submit = agent_api.submit_signal_room_task

    def _spy(task, task_dir, confine, **kw):
        dispatched.append((task, kw))
        return orig_submit(task, task_dir, confine, **kw)

    agent_api.submit_signal_room_task = _spy
    orig_emit = getattr(agent_api, "_emit_task_processed", None)
    agent_api._emit_task_processed = lambda *a, **k: None
    try:
        # --- POST /guest-task: the route decides the tier -----------------------
        dispatched.clear()
        h = post("/guest-task", {"task": "what happened with item 2?"})
        check("/guest-task -> 200", bool(h._responses) and h._responses[0][0] == 200)
        check("/guest-task submits one Signal Room task", len(dispatched) == 1)
        tid = h._responses[0][1].get("task_id", "") if h._responses else ""
        check("/guest-task id is a canonical task-signal-* id", tid.startswith("task-signal-"))
        check("/guest-task result_url points at the async contract",
              (h._responses[0][1].get("result_url") or "") == f"/result/{tid}")

        # A body cannot escalate itself on the guest route.
        dispatched.clear()
        h = post("/guest-task", {"task": "escalate me", "access_tier": "owner"})
        gid = h._responses[0][1].get("task_id", "") if h._responses else ""
        check("/guest-task ignores a body-declared owner tier (route wins)",
              h._responses[0][0] == 200 and gid.startswith("task-signal-") and len(dispatched) == 1)

        # Missing/blank task -> 400, worker never invoked.
        dispatched.clear()
        h = post("/guest-task", {"task": "   "})
        check("/guest-task blank task -> 400", h._responses[0][0] == 400)
        check("/guest-task blank task is never submitted", dispatched == [])

        # Size guard runs BEFORE the body is read (same as the owner lane).
        dispatched.clear()
        h = make_handler("/guest-task", {"Content-Length": "1000000"})
        h.do_POST()
        check("/guest-task oversized -> 413", h._responses[0][0] == 413)
        check("/guest-task oversized: body never read", h.rfile.read_calls == 0)
        check("/guest-task oversized: nothing submitted", dispatched == [])

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

        def drain_slots():
            """Free admission slots so routing checks aren't answered with 429."""
            for stale in Path(agent_api.TASK_DIR).glob("task-signal-*.txt"):
                stale.unlink()

        # --- POST /task: demote-only shim, never escalation ---------------------
        dispatched.clear()
        drain_slots()
        h = post("/task", {"from": "matrixrtc-voice-news", "task": "shipped daemon",
                           "access_tier": "guest"})
        stid = h._responses[0][1].get("task_id", "") if h._responses else ""
        check("/task with access_tier guest still routes to the Signal Room lane (compat shim)",
              h._responses[0][0] == 200 and len(dispatched) == 1 and stid.startswith("task-signal-"))

        for tier in ("owner", "team", "admin", "", "GUEST"):
            dispatched.clear()
            h = post("/task", {"from": "x", "task": "t", "access_tier": tier})
            check(f"/task refuses access_tier={tier!r} (no silent owner fallthrough)",
                  h._responses[0][0] == 400 and dispatched == [])

        # --- admission bound ------------------------------------------------------
        import os as _os
        import time as _time
        import signal_room_tasks as _srt
        drain_slots()
        dispatched.clear()
        codes = [post("/guest-task", {"task": f"q{i}"})._responses[0][0]
                 for i in range(_srt.MAX_OUTSTANDING + 1)]
        check("/guest-task admits up to MAX_OUTSTANDING",
              codes[:_srt.MAX_OUTSTANDING] == [200] * _srt.MAX_OUTSTANDING)
        check("/guest-task over the bound -> 429, not 500",
              codes[-1] == 429)
        check("/guest-task over the bound writes no task file",
              len(list(Path(agent_api.TASK_DIR).glob("task-signal-*.txt")))
              == _srt.MAX_OUTSTANDING)

        # A task whose core died is never cleaned up by anyone; if it held its
        # slot forever the lane would wedge shut permanently.
        stranded = _time.time() - (_srt.SLOT_TTL_SEC + 60)
        for orphan in Path(agent_api.TASK_DIR).glob("task-signal-*.txt"):
            _os.utime(orphan, (stranded, stranded))
        h = post("/guest-task", {"task": "after the orphans"})
        check("stranded tasks age out so the lane reopens", h._responses[0][0] == 200)

        # --- GET /capabilities ---------------------------------------------------
        def get(path: str, auth: bool = True):
            h = make_handler(path, {}, auth=auth)
            h.do_GET()
            return h

        orig_avail = agent_api.submission_status
        agent_api.submission_status = lambda *a, **k: (True, None)
        h = get("/capabilities")
        body = h._responses[0][1] if h._responses else {}
        check("/capabilities -> 200", bool(h._responses) and h._responses[0][0] == 200)
        check("/capabilities reports available:true when the lane is ready",
              body.get("guest_deep_dive", {}).get("available") is True)
        check("/capabilities omits reason when ready",
              "reason" not in body.get("guest_deep_dive", {}))

        agent_api.submission_status = lambda *a, **k: (False, "task_dir_unwritable")
        h = get("/capabilities")
        cap = h._responses[0][1].get("guest_deep_dive", {}) if h._responses else {}
        check("/capabilities reports available:false with the machine-readable reason",
              cap.get("available") is False and cap.get("reason") == "task_dir_unwritable")

        def boom(*a, **k):
            raise RuntimeError("lane exploded")

        agent_api.submission_status = boom
        h = get("/capabilities")
        cap = h._responses[0][1].get("guest_deep_dive", {}) if h._responses else {}
        check("/capabilities fails closed when the lane raises",
              h._responses[0][0] == 200 and cap.get("available") is False
              and str(cap.get("reason", "")).startswith("capability_error"))

        h = get("/capabilities", auth=False)
        check("/capabilities honours check_auth",
              not any(r[0] == 200 for r in h._responses))

        agent_api.submission_status = orig_avail

        # --- the egress boundary on the way back out ----------------------------
        # A Team result is untrusted output: scanned on BOTH read paths.
        drain_slots()
        secret = "token sk-ant-api03-" + "A" * 80
        rdir = Path(agent_api.RESULT_DIR)
        rdir.mkdir(parents=True, exist_ok=True)

        tid = post("/guest-task", {"task": "research"})._responses[0][1]["task_id"]
        (rdir / f"{tid}.txt").write_text(secret)
        live = agent_api.get_task_result(tid)["result"]
        check("a live team result is scanned before release", secret not in live)

        # Archive both halves exactly as task-bridge does, leaving nothing live.
        arc_r = rdir / "archive" / "2026-08"
        arc_r.mkdir(parents=True, exist_ok=True)
        (arc_r / f"{tid}.txt").write_text(secret)
        (rdir / f"{tid}.txt").unlink()
        arc_t = Path(agent_api.TASK_DIR) / "archive" / "2026-08"
        arc_t.mkdir(parents=True, exist_ok=True)
        (arc_t / f"{tid}.txt").write_text(f"id: {tid}\naccess_tier: team\ntask: x\n")
        (Path(agent_api.TASK_DIR) / f"{tid}.txt").unlink()
        archived = agent_api.get_task_result(tid)["result"]
        check("an archived team result is scanned too (the common room poll)",
              secret not in archived)

        # No metadata at all: a Signal Room id is team by construction.
        orphan = "task-signal-9999999999999-deadbeef"
        (arc_r / f"{orphan}.txt").write_text(secret)
        check("a room result with no surviving task file is still scanned",
              secret not in agent_api.get_task_result(orphan)["result"])

        # The owner's own work is not someone else's untrusted output.
        otid = "task-owner-passthrough"
        (Path(agent_api.TASK_DIR) / f"{otid}.txt").write_text(
            f"id: {otid}\naccess_tier: owner\ntask: x\n")
        (rdir / f"{otid}.txt").write_text("owner plain text")
        check("an owner result is returned unchanged",
              agent_api.get_task_result(otid)["result"] == "owner plain text")

        # Classification failure must withhold, never fall through.
        orig_resolve = sys.modules["policy.egress.result"].resolve_access_tier

        def _explode(*a, **k):
            raise RuntimeError("classifier down")

        sys.modules["policy.egress.result"].resolve_access_tier = _explode
        try:
            (rdir / f"{otid}.txt").write_text(secret)
            withheld = agent_api.get_task_result(otid)["result"]
            check("a classifier failure withholds the body", secret not in withheld)
        finally:
            sys.modules["policy.egress.result"].resolve_access_tier = orig_resolve

        # The compat shim answers a full lane with 429, not 500.
        drain_slots()
        for i in range(_srt.MAX_OUTSTANDING):
            post("/task", {"from": "matrixrtc-voice-news", "task": f"fill{i}",
                           "access_tier": "guest"})
        h = post("/task", {"from": "matrixrtc-voice-news", "task": "over",
                           "access_tier": "guest"})
        check("/task compat shim over the bound -> 429", h._responses[0][0] == 429)
        drain_slots()
        post("/guest-task", {"task": "restore one task file for the closing check"})

        # --- the decisive property: no owner task from any guest submission ------
        # Signal Room work now lands as a normal task — at TEAM tier, never owner.
        signal_files = list(Path(agent_api.TASK_DIR).glob("task-signal-*.txt"))
        check("Signal Room submissions produced task files", len(signal_files) >= 1)
        tiers = set()
        for f in signal_files:
            for line in f.read_text().split("\n"):
                if line.startswith("access_tier:"):
                    tiers.add(line.partition(":")[2].strip())
                if line.startswith("task:"):
                    break
        check(f"every Signal Room task is team tier, never owner (saw {tiers})", tiers == {"team"})
    finally:
        agent_api.submit_signal_room_task = orig_submit
        if orig_emit is not None:
            agent_api._emit_task_processed = orig_emit

    # ── JSON bodies that parse but are not objects, and bodies that do not parse
    #    as UTF-8 at all, must be 400s on BOTH lanes (worker-3, 2026-09-02) ──
    def raw(path: str, body: bytes):
        h = make_handler(path, {"Content-Length": str(len(body))}, body, auth=True)
        try:
            h.do_POST()
        except Exception as e:  # the parent 500s here; report it as a value
            return f"raised {type(e).__name__}"
        return h._responses[0][0] if h._responses else None
    check("/guest-task: a JSON array body is a 400, not a 500", raw("/guest-task", b"[1]") == 400)
    check("/guest-task: a JSON string body is a 400, not a 500", raw("/guest-task", b'"hi"') == 400)
    # NEGATIVE CONTROL: the guest lane already catches bare Exception around the
    # decode, so this is green before AND after — it pins that the lanes differ.
    check("/guest-task: invalid UTF-8 is a 400 (negative control)", raw("/guest-task", b"\xff\xfe{") == 400)
    check("/task: a JSON array body is a 400, not a 500", raw("/task", b"[1]") == 400)
    # UnicodeDecodeError is not a JSONDecodeError subclass; this one was a 500.
    check("/task: invalid UTF-8 is a 400, not a 500", raw("/task", b"\xff\xfe{") == 400)

    print()
    if failures:
        print(f"FAILED ({failures})")
        sys.exit(1)
    print("all ok")


run()
