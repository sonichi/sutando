#!/usr/bin/env python3
"""A replayed router pass finishes the delivery it already committed to.

The watcher re-runs the router on any task still in tasks/ after a watcher
died mid-handler (#4825), so a pass must be idempotent for the TASK, not for
one recipient folder:

- a room rebound between an interrupted delivery and its replay must not
  deliver a second time to the new binding: the committed recipient wins;
- a crash between writing the sentinel and writing the attribution leaves the
  fact without the record; the replay must repair it, not report `already`
  and walk on.

Both cases fail on the parent: the first delivers to B as well, the second
leaves the attribution absent after the retry.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

import pool_attribution as pa  # noqa: E402
import pool_router as rt  # noqa: E402

A = "a" * 32
B = "b" * 32
ROOM = "!bound:example.test"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def workspace(bound_to: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="router-replay-"))
    ws = tmp / "ws"
    for d in ("tasks", "state", "deliveries"):
        (ws / d).mkdir(parents=True)
    bind(ws, bound_to)
    (ws / "tasks" / "task-r.txt").write_text(
        f"id: task-r\nchannel_id: {ROOM}\nsource: ag2space\naccess_tier: owner\ntask: body\n")
    return ws


def bind(ws: Path, worker: str) -> None:
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"version": 1, "workers": {A: {"state": "live"}, B: {"state": "live"}},
         "bindings": {ROOM: worker}}))


def task(ws: Path) -> dict:
    return {"id": "task-r", "channel_id": ROOM, "source": "ag2space"}


def sentinels(ws: Path) -> list[str]:
    return sorted(str(p.relative_to(ws)) for p in (ws / "deliveries").glob("*/task-r*"))


def scenario_rebind_between_delivery_and_replay() -> None:
    print("\nscenario: bound to A, delivered, rebound to B, replayed")
    ws = workspace(A)
    first = rt.route(ws, task(ws))
    check("first pass delivers to A", first["delivered"] == [A], str(first))
    bind(ws, B)
    second = rt.route(ws, task(ws))
    check("the replay reports the committed recipient A as already delivered",
          second["already"] == [A] and second["delivered"] == [], str(second))
    check("exactly one sentinel exists, in A's folder",
          sentinels(ws) == [f"deliveries/{A}/task-r.txt"], str(sentinels(ws)))
    check("attribution still names A", pa.worker_for_task(ws, "task-r") == A,
          str(pa.worker_for_task(ws, "task-r")))


def scenario_kill_between_sentinel_and_attribution() -> None:
    print("\nscenario: the writer dies after the sentinel, before the attribution")
    ws = workspace(A)
    code = (
        "import sys, os; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
        "import pool_router as rt\n"
        "rt.deliver_one(%r, %r, 'task-r', _between_sentinel_and_attribution=lambda: os._exit(9))\n"
        % (str(SCRIPTS), str(REPO / "src"), str(ws), A))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    check("the production writer died at the seam", p.returncode == 9, f"rc={p.returncode} {p.stderr[-200:]}")
    check("...leaving the sentinel", sentinels(ws) == [f"deliveries/{A}/task-r.txt"], str(sentinels(ws)))
    check("...and no attribution", pa.worker_for_task(ws, "task-r") is None)
    retry = rt.route(ws, task(ws))
    check("the replay reports already, not a second delivery",
          retry["already"] == [A] and retry["delivered"] == [], str(retry))
    check("and the attribution is repaired to A", pa.worker_for_task(ws, "task-r") == A,
          str(pa.worker_for_task(ws, "task-r")))
    check("still exactly one sentinel", sentinels(ws) == [f"deliveries/{A}/task-r.txt"], str(sentinels(ws)))


def scenario_plain_replay_is_still_already() -> None:
    print("\nscenario: control — same binding, replayed twice")
    ws = workspace(A)
    rt.route(ws, task(ws))
    again = rt.route(ws, task(ws))
    check("the second pass reports already for A", again["already"] == [A] and again["delivered"] == [], str(again))
    check("one sentinel", sentinels(ws) == [f"deliveries/{A}/task-r.txt"], str(sentinels(ws)))


def scenario_handler_probe_follows_the_commit() -> None:
    print("\nscenario: the handler's probe after a rebind, and after an unbind, still accepts for A")
    import pool_route_handler as h
    ws = workspace(A)
    rt.route(ws, task(ws))
    bind(ws, B)
    code, targets, _ = h.classify(ws, task(ws))
    check("rebound to B: the probe accepts and names A", (code, targets) == (0, [A]), str((code, targets)))
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"version": 1, "workers": {A: {"state": "live"}, B: {"state": "live"}}, "bindings": {}}))
    code, targets, _ = h.classify(ws, task(ws))
    check("unbound: the probe still accepts and names A (the core must not inherit A's task)",
          (code, targets) == (0, [A]), str((code, targets)))
    ws2 = workspace(A)
    code, targets, _ = h.classify(ws2, task(ws2))
    check("control: a task nobody holds yet is classified by the roster", (code, targets) == (0, [A]), str((code, targets)))


def seed(ws: Path, recipient: str, suffix: str, attribution: str | None) -> None:
    d = ws / "deliveries" / recipient
    d.mkdir(parents=True, exist_ok=True)
    (d / f"task-r{suffix}").touch()
    if attribution:
        pa.record(ws, "task-r", attribution)


def scenario_every_suffix_and_every_attribution_shape() -> None:
    print("\nscenario: the commit is found under every production suffix, matched against the record")
    for suffix in (".txt", ".accepted", ".claimed"):
        ws = workspace(B)                         # bound to B now; the fact is in A
        seed(ws, A, suffix, None)
        out = rt.route(ws, task(ws))
        check(f"{suffix}: a unique sentinel with no record wins over the binding and is attributed",
              out["already"] == [A] and out["delivered"] == [] and pa.worker_for_task(ws, "task-r") == A, str(out))
        check(f"{suffix}: no sentinel was created for B", not (ws / "deliveries" / B / "task-r.txt").exists())
    ws = workspace(B)
    seed(ws, A, ".txt", A)
    out = rt.route(ws, task(ws))
    check("matching record and fact: already for A", out["already"] == [A], str(out))
    ws = workspace(B)
    seed(ws, A, ".txt", B)                        # the record names B, the fact is in A
    try:
        rt.route(ws, task(ws))
        check("conflicting record and fact: refused", False, "route returned")
    except rt.ConflictingDelivery as e:
        check("conflicting record and fact: refused and named", "attribution=" in str(e) and A in str(e), str(e))
    check("...and nothing was created", sentinels(ws) == [f"deliveries/{A}/task-r.txt"], str(sentinels(ws)))
    ws = workspace(B)
    seed(ws, A, ".txt", None)
    seed(ws, B, ".accepted", None)                # two holders
    try:
        rt.route(ws, task(ws))
        check("two holders: refused", False, "route returned")
    except rt.ConflictingDelivery as e:
        check("two holders: refused and both named", A in str(e) and B in str(e), str(e))
    import pool_route_handler as h
    code, targets, _ = h.classify(ws, task(ws))
    check("...and the handler's probe answers must-handle, so the anomaly surfaces", code == h.MUST_HANDLE, str((code, targets)))


def scenario_concurrent_passes_with_different_bindings() -> None:
    print("\nscenario: two passes race the same task, one seeing A bound and one seeing B")
    ws = workspace(A)
    code = (
        "import sys, json, time, os; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
        "import pool_router as rt\n"
        "roster = {'version': 1, 'workers': {%r: {'state': 'live'}, %r: {'state': 'live'}}, 'bindings': {%r: sys.argv[1]}}\n"
        "start = float(sys.argv[2])\n"
        "while time.time() < start: time.sleep(0.001)\n"
        "out = rt.route(%r, {'id': 'task-r', 'channel_id': %r, 'source': 'ag2space'}, roster)\n"
        "print(json.dumps(out))\n"
        % (str(SCRIPTS), str(REPO / "src"), A, B, ROOM, str(ws), ROOM))
    import time
    start = str(time.time() + 1.0)
    ps = [subprocess.Popen([sys.executable, "-c", code, w, start], stdout=subprocess.PIPE, text=True) for w in (A, B)]
    outs = [json.loads(p.communicate()[0].strip().splitlines()[-1]) for p in ps]
    delivered = [o["delivered"] for o in outs]
    already = [o["already"] for o in outs]
    check("exactly one pass delivered", sum(len(d) for d in delivered) == 1, str(outs))
    winner = [d[0] for d in delivered if d][0] if any(delivered) else None
    check("the other pass reported the winner as already", already == [[]] * 0 or any(a == [winner] for a in already), str(outs))
    check("one sentinel across both folders", len(sentinels(ws)) == 1 and winner in sentinels(ws)[0], str(sentinels(ws)))
    check("the attribution names the winner", pa.worker_for_task(ws, "task-r") == winner, str(pa.worker_for_task(ws, "task-r")))


def main() -> int:
    scenario_plain_replay_is_still_already()
    scenario_every_suffix_and_every_attribution_shape()
    scenario_concurrent_passes_with_different_bindings()
    scenario_handler_probe_follows_the_commit()
    scenario_rebind_between_delivery_and_replay()
    scenario_kill_between_sentinel_and_attribution()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nPASS — a replayed pass follows the committed delivery and repairs its attribution")
    return 0


if __name__ == "__main__":
    sys.exit(main())
