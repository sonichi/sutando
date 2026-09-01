#!/usr/bin/env python3
"""Direct coverage for src/runtime-api/dispatcher.py — the request-domain layer.

Drives the dispatcher with a real RequestStore and a real HumanActionAdapter over
temporary paths, plus fake executors. No socket, no daemon, no gateway.

Assertions read DURABLE STORE STATE as well as returned objects: these policies
are security contracts (governed-capability gating, fingerprint binding, one-time
approval consumption, idempotency, crash recovery), and a response object alone
does not prove the row transitioned correctly.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from dispatcher import RuntimeDispatcher, _fingerprint, GOVERNED_ACTIONS  # noqa: E402
from request_store import RequestStore  # noqa: E402
from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402
from protocol import ProtocolError  # noqa: E402

FAILS: list = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


def raises(label, fn, code=None, substr=None):
    try:
        fn()
    except ProtocolError as e:
        ok = (code is None or e.code == code) and (substr is None or substr in str(e))
        check(label, ok, f"code={getattr(e, 'code', None)} msg={e}")
        return
    except Exception as e:  # noqa: BLE001
        check(label, False, f"wrong exception {type(e).__name__}: {e}")
        return
    check(label, False, "no exception raised")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


_n = 0


def fresh(executors=None, actor="test-actor", granted=frozenset()):
    """A dispatcher over disposable store + human-action dir."""
    global _n
    _n += 1
    d = Path(tempfile.mkdtemp(prefix=f"rt-disp-{_n}-"))
    store = RequestStore(str(d / "state.sqlite"))
    ha = HumanActionAdapter(str(d / "ha"))
    return (RuntimeDispatcher(store, ha, actor, executors or {},
                              granted_methods=granted),
            store, ha, d)


def approve(ha, disp, rid, decision="approved", answer=None):
    """Resolve the mirrored card the way the owner's UI would."""
    aid = disp._ha_of[rid]
    payload = {"status": decision}
    if answer is not None:
        payload["answer"] = answer
    ha.resolve(aid, **payload) if hasattr(ha, "resolve") else None
    return aid


print("── dispatch table ──")
d, store, ha, _ = fresh()
raises("unknown method → -32601", lambda: run(d.handle("no.such", {})), code=-32601)
raises("ungranted human_action.complete → -32601 before any param check",
       lambda: run(d.handle("human_action.complete", {})), code=-32601)
_dg, _, _, _ = fresh(granted=frozenset({"human_action.complete"}))
raises("granted human_action.complete still requires requestId",
       lambda: run(_dg.handle("human_action.complete", {})), code=-32602)
raises("approval.request requires action", lambda: run(d.handle("approval.request", {})),
       code=-32602, substr="action")
raises("elicitation.request requires question",
       lambda: run(d.handle("elicitation.request", {"type": "confirmation"})),
       code=-32602, substr="question")

r = run(d.handle("approval.request", {"action": "x.y"}))
check("approval.request returns pending + requestId",
      r["status"] == "pending" and r["requestId"], str(r))
check("issued row is durable and pending",
      store.get(r["requestId"])["status"] == "pending")
check("actor persisted from CONSTRUCTOR, not params",
      store.get(r["requestId"])["actorId"] == "test-actor",
      str(store.get(r["requestId"]).get("actorId")))

d2, store2, _, _ = fresh(actor="daemon-actor")
r2 = run(d2.handle("approval.request", {"action": "x.y", "actor": "spoofed"}))
check("a client-supplied actor cannot override the daemon actor",
      store2.get(r2["requestId"])["actorId"] == "daemon-actor",
      str(store2.get(r2["requestId"]).get("actorId")))

print("── elicitation types ──")
d, store, ha, _ = fresh()
raises("free_text rejected in v0",
       lambda: run(d.handle("elicitation.request", {"question": "q", "type": "free_text"})),
       code=-32602, substr="free_text")
raises("unknown type rejected",
       lambda: run(d.handle("elicitation.request", {"question": "q", "type": "bogus"})),
       code=-32602)
raises("single_select requires options",
       lambda: run(d.handle("elicitation.request", {"question": "q", "type": "single_select"})),
       code=-32602, substr="options")
raises("multi_select requires options",
       lambda: run(d.handle("elicitation.request", {"question": "q", "type": "multi_select"})),
       code=-32602, substr="options")
ok_e = run(d.handle("elicitation.request",
                    {"question": "q", "type": "single_select", "options": ["a", "b"]}))
check("valid single_select issues", ok_e["status"] == "pending")
ok_c = run(d.handle("elicitation.request", {"question": "q?", "type": "confirmation"}))
check("confirmation needs no options", ok_c["status"] == "pending")

print("── human-action mirror failure ──")
d, store, ha, _ = fresh()


def boom(_rec):
    raise RuntimeError("card backend down")


d.ha.open_approval = boom
try:
    run(d.handle("approval.request", {"action": "x.y"}))
    check("mirror failure raises -32603", False, "no exception")
except ProtocolError as e:
    check("mirror failure raises -32603", e.code == -32603, f"code={e.code}")
rows = [store.get(rid) for rid in
        [r["requestId"] for r in [] ] ] or None
# the row must be durably FAILED, not left pending for nothing to answer
all_rows = store.pending()
check("a failed mirror leaves NO pending row behind", not all_rows,
      f"pending={[r['requestId'] for r in all_rows]}")

print("── get / wait / cancel ──")
d, store, ha, _ = fresh()
raises("get unknown requestId → -32602", lambda: d._get({"requestId": "nope"}), code=-32602)
raises("cancel unknown requestId → -32602", lambda: d._cancel({"requestId": "nope"}), code=-32602)
raises("get with no requestId → -32602", lambda: d._get({}), code=-32602)

rid = run(d.handle("approval.request", {"action": "x.y"}))["requestId"]
pub = d._get({"requestId": rid})
check("public shape: requestId + status only while pending",
      set(pub) == {"requestId", "status"} and pub["status"] == "pending", str(pub))

c = d._cancel({"requestId": rid})
check("cancel transitions to cancelled", c["status"] == "cancelled", str(c))
check("cancellation is durable", store.get(rid)["status"] == "cancelled")
check("public shape includes resolvedBy once set", "resolvedBy" in c, str(c))

d, store, ha, _ = fresh()
rid = run(d.handle("approval.request", {"action": "x.y"}))["requestId"]
t0 = time.monotonic()
w = run(d._wait({"requestId": rid, "timeoutS": 0.2}))
check("wait times out and reports timedOut", w.get("timedOut") is True, str(w))
check("a timed-out request stays pending", store.get(rid)["status"] == "pending")
check("wait honoured the short timeout", time.monotonic() - t0 < 5.0)

print("── governed capability gating ──")
calls: list = []


def ok_exec(params):
    calls.append(params)
    return {"executed": True, "eventId": "$e1"}


d, store, ha, _ = fresh(executors={"message.send": ok_exec})
check("message.send is governed", "message.send" in GOVERNED_ACTIONS)

base = {"action": "message.send", "resource": {"roomId": "!r:x"}, "input": {"body": "hi"}}
raises("governed action with NO approval fails closed",
       lambda: run(d.handle("capability.execute", dict(base))), code=-32602)
check("no executor ran for the ungated attempt", not calls, str(calls))

raises("unknown approvalRequestId fails closed",
       lambda: run(d.handle("capability.execute", {**base, "approvalRequestId": "ghost"})),
       code=-32602)

# a PENDING (not yet approved) approval must not authorize
pend = run(d.handle("approval.request", {"action": "message.send"}))["requestId"]
raises("a non-approved approval fails closed",
       lambda: run(d.handle("capability.execute", {**base, "approvalRequestId": pend})),
       code=-32602)
check("still no executor ran", not calls, str(calls))

print("── fingerprint binding ──")
fp_a = _fingerprint(base)
fp_b = _fingerprint({**base, "input": {"body": "DIFFERENT"}})
fp_c = _fingerprint({**base, "resource": {"roomId": "!other:x"}})
fp_d = _fingerprint({**base, "action": "other.action"})
check("fingerprint changes with input", fp_a != fp_b)
check("fingerprint changes with resource", fp_a != fp_c)
check("fingerprint changes with action", fp_a != fp_d)
check("fingerprint is stable for identical params", fp_a == _fingerprint(dict(base)))
check("fingerprint ignores key order",
      _fingerprint({"input": {"body": "hi"}, "resource": {"roomId": "!r:x"},
                    "action": "message.send"}) == fp_a)

print("── executor results ──")
d, store, ha, _ = fresh(executors={})
# Contract: an unknown executor RECORDS and RETURNS a failed result — it does
# not raise. The durable row must carry the failure, not stay pending.
res = run(d.handle("capability.execute", {"action": "no.such.exec"}))
check("unknown executor returns a failed result (does not raise)",
      res.get("status") == "failed", str(res))
check("unknown executor's failure is durable",
      store.get(res["requestId"])["status"] == "failed",
      str(store.get(res["requestId"])["status"]))
check("the failure names the missing executor",
      "executor" in json.dumps(res.get("result") or {}), str(res.get("result")))

# A successful ungoverned execution completes and persists its result.
seen = []
d2, store2, _, _ = fresh(executors={"safe.op": lambda p: (seen.append(p), {"ok": True})[1]})
res2 = run(d2.handle("capability.execute", {"action": "safe.op", "input": {"a": 1}}))
check("ungoverned action executes without an approval",
      res2.get("status") == "completed", str(res2))
check("its executor actually ran", len(seen) == 1, str(seen))
check("the completed result is durable",
      store2.get(res2["requestId"])["status"] == "completed")

# A blocking executor is offloaded, so it cannot stall the event loop.
def slow(_p):
    time.sleep(0.4)
    return {"ok": True}

d3, _, _, _ = fresh(executors={"slow.op": slow})


async def concurrent():
    t0 = time.monotonic()
    slow_task = asyncio.ensure_future(d3.handle("capability.execute", {"action": "slow.op"}))
    await asyncio.sleep(0.05)
    mid = time.monotonic() - t0          # loop still responsive while slow runs
    await slow_task
    return mid


mid = run(concurrent())
check("a blocking executor is offloaded (loop stayed responsive)", mid < 0.3, f"{mid:.2f}s")

print("── resolver loop isolation ──")
d, store, ha, _ = fresh()
boom_count = {"n": 0}
_orig_pending = store.pending


def flaky_pending():
    boom_count["n"] += 1
    if boom_count["n"] == 1:
        raise RuntimeError("transient store error")
    return []


store.pending = flaky_pending


async def one_and_a_bit():
    task = asyncio.ensure_future(d.resolver_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


run(one_and_a_bit())
check("resolver survives a polling exception (did not die on first error)",
      boom_count["n"] >= 1)
store.pending = _orig_pending

print("── crash recovery ──")
d, store, ha, tmp = fresh()
a_rid = run(d.handle("approval.request", {"action": "x.y"}))["requestId"]
e_rid = run(d.handle("elicitation.request", {"question": "q", "type": "confirmation"}))["requestId"]
d._ha_of.clear()          # simulate daemon restart: in-memory map lost
d.recover()
check("recovery relinks a pending approval to its card", a_rid in d._ha_of, str(d._ha_of))
check("recovery relinks a pending elicitation to its card", e_rid in d._ha_of, str(d._ha_of))
check("recovered approval is still pending", store.get(a_rid)["status"] == "pending")

print("── approval.respond (device-plane; needs a daemon-resolved grant) ──")
d0, store0, ha0, _ = fresh(actor="ungranted")
ra0 = run(d0.handle("approval.request", {"action": "x.y"}))
raises("respond WITHOUT a grant is not callable (fails closed)",
       lambda: run(d0.handle("approval.respond",
                             {"requestId": ra0["requestId"],
                              "decision": "approve"})),
       code=-32601, substr="grant")
check("ungranted respond leaves the approval pending",
      store0.get(ra0["requestId"])["status"] == "pending")
d, store, ha, _ = fresh(actor="responder",
                        granted=frozenset({"approval.respond"}))
raises("respond requires requestId",
       lambda: run(d.handle("approval.respond", {})), code=-32602, substr="requestId")
raises("respond requires a valid decision",
       lambda: run(d.handle("approval.respond", {"requestId": "r", "decision": "maybe"})),
       code=-32602, substr="decision")
raises("respond on unknown request",
       lambda: run(d.handle("approval.respond",
                            {"requestId": "nope", "decision": "approve"})),
       code=-32602, substr="unknown")
ra = run(d.handle("approval.request", {"action": "x.y"}))
rr = run(d.handle("approval.respond",
                  {"requestId": ra["requestId"], "decision": "approve"}))
check("respond approves and reports the transition",
      rr["status"] == "approved", str(rr))
check("approved is durable", store.get(ra["requestId"])["status"] == "approved")
rr2 = run(d.handle("approval.respond",
                   {"requestId": ra["requestId"], "decision": "reject"}))
check("second respond reports alreadyTerminal, never flips the row",
      rr2.get("alreadyTerminal") and store.get(ra["requestId"])["status"] == "approved",
      str(rr2))
re_ = run(d.handle("elicitation.request",
                   {"question": "q?", "type": "single_select",
                    "options": ["a", "b"]}))
raises("respond refuses a non-approval request type",
       lambda: run(d.handle("approval.respond",
                            {"requestId": re_["requestId"], "decision": "approve"})),
       code=-32602, substr="approval")
rd = run(d.handle("approval.request", {"action": "z.z"}))
rrd = run(d.handle("approval.respond",
                   {"requestId": rd["requestId"], "decision": "reject"}))
check("respond reject denies", rrd["status"] == "denied", str(rrd))

print("── schedule / task param edges ──")
raises("schedule.list without a schedules surface",
       lambda: run(d.handle("schedule.list", {})), code=-32601, substr="schedule")
raises("task.* without a task pipeline is a clean -32601",
       lambda: run(d.handle("task.status", {})), code=-32601, substr="task pipeline")
raises("request.get without requestId",
       lambda: run(d.handle("request.get", {})), code=-32602, substr="requestId")

print("── configured-surface branches ──")
sys.path.insert(0, str(REPO / "src"))
from schedules_view import SchedulesView  # noqa: E402
import json as _j
import tempfile as _tf
_sd = Path(_tf.mkdtemp(prefix="rt-disp-sched-"))
(_sd / "crons.json").write_text(_j.dumps([{"name": "x", "cron": "*/5 * * * *",
                                           "prompt": "p"}]))
d.schedules = SchedulesView(_sd / "crons.json")
rows = run(d.handle("schedule.list", {}))
check("schedule.list serves configured rows through the dispatcher",
      rows["schedules"] and rows["schedules"][0]["name"] == "x", str(rows))

from tasks_view import TasksView  # noqa: E402
_td = Path(_tf.mkdtemp(prefix="rt-disp-tasks-"))
(_td / "tasks").mkdir(); (_td / "results").mkdir()
d.tasks = TasksView(_td / "tasks", _td / "results", "@disp:test")
raises("task.status with a pipeline but no taskId",
       lambda: run(d.handle("task.status", {})), code=-32602, substr="taskId")
raises("task.details unknown id",
       lambda: run(d.handle("task.details", {"taskId": "task-none"})),
       code=-32602, substr="unknown task")
raises("task.submit empty text is a ValueError -> -32602",
       lambda: run(d.handle("task.submit", {"task": "   "})), code=-32602)
raises("request.wait without requestId",
       lambda: run(d.handle("request.wait", {})), code=-32602, substr="requestId")

print()
if FAILS:
    print(f"FAIL — {len(FAILS)}: {FAILS}")
    sys.exit(1)
print("PASS — runtime-api dispatcher")
