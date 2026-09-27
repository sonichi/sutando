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

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

import pool_attribution as pa  # noqa: E402

import pool_delivery as pd  # noqa: E402
import pool_router as rt  # noqa: E402

A = "a" * 32
B = "b" * 32
ROOM = "!bound:example.test"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


_TREES: list[Path] = []
atexit.register(lambda: [shutil.rmtree(t, ignore_errors=True) for t in _TREES])


def workspace(bound_to: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="router-replay-"))
    _TREES.append(tmp)
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


def scenario_unreadable_and_malformed_evidence_refuse() -> None:
    print("\nscenario: unreadable or malformed evidence refuses instead of reading as absent")
    import pool_route_handler as h
    ws = workspace(B)
    seed(ws, A, ".txt", None)
    folder = ws / "deliveries" / A
    folder.chmod(0o000)                          # A's real sentinel temporarily unreadable
    try:
        try:
            rt.route(ws, task(ws))
            check("unreadable sentinel folder: refused", False, "route returned")
        except rt.UnreadableEvidence as e:
            check("unreadable sentinel folder: refused and named", "refusing" in str(e) or "cannot lock" in str(e), str(e))
        code, targets, _ = h.classify(ws, task(ws))
        check("...and the handler's probe answers must-handle", code == h.MUST_HANDLE, str((code, targets)))
    finally:
        folder.chmod(0o755)
    check("...and nothing was created for B meanwhile", not (ws / "deliveries" / B / "task-r.txt").exists())
    after = rt.route(ws, task(ws))
    check("readable again: the replay reports already for A", after["already"] == [A] and after["delivered"] == [], str(after))
    ws = workspace(B)
    seed(ws, A, ".txt", None)
    pa.attribution_dir(ws).mkdir(parents=True, exist_ok=True)
    pa.attribution_path(ws, "task-r").write_text("not-a-worker-id\n")
    try:
        rt.route(ws, task(ws))
        check("malformed attribution: refused", False, "route returned")
    except rt.ConflictingDelivery as e:
        check("malformed attribution: refused, not reported as repaired", "malformed" in str(e), str(e))
    check("...and the malformed record is left for a human", pa.attribution_path(ws, "task-r").read_text().strip() == "not-a-worker-id")


def scenario_commit_outlives_the_roster() -> None:
    print("\nscenario: after a committed delivery to A, the roster disappears; the replay still finishes it")
    import pool_route_handler as h
    ws = workspace(A)
    rt.route(ws, task(ws))
    (ws / "state" / "roster.json").unlink()
    out = rt.route(ws, task(ws))
    check("replay without a roster reports already for A", out["already"] == [A] and out["delivered"] == [] and out["targets"] == [A], str(out))
    code, targets, _ = h.classify(ws, task(ws))
    check("the probe accepts for A without a roster", (code, targets) == (0, [A]), str((code, targets)))
    rc = h.main(["--task-file", str(ws / "tasks" / "task-r.txt"), "--workspace", str(ws)])
    check("the handler's run settles (rc 0), judged by the route's committed target", rc == 0, str(rc))


def scenario_handler_settles_by_the_routes_targets() -> None:
    print("\nscenario: the probe saw B bound and nothing committed; A commits INSIDE the run's window; the run settles on A")
    import pool_route_handler as h
    ws = workspace(B)
    code, targets, _ = h.classify(ws, task(ws))
    check("probe snapshot names B", (code, targets) == (0, [B]), str((code, targets)))
    # Wrap the handler's route dependency: A commits on entry, then the real route
    # runs. That lands A between main()'s own classification (B) and its route.
    real_route = h.rt.route

    def committing_route(workspace, task_dict, roster=None, **kw):
        seed(ws, A, ".txt", A)
        return real_route(workspace, task_dict, roster, **kw)
    h.rt.route = committing_route
    try:
        rc = h.main(["--task-file", str(ws / "tasks" / "task-r.txt"), "--workspace", str(ws)])
    finally:
        h.rt.route = real_route
    check("the run reports settled (rc 0): A already owns it, B is not a missing delivery", rc == 0, str(rc))
    check("no sentinel for B", not (ws / "deliveries" / B / "task-r.txt").exists())


def scenario_release_race_is_serialised_by_the_folder_lock() -> None:
    print("\nscenario: A holds task-r.accepted (crash residue, no record); a production release races the replay's reads")
    ws = workspace(B)
    seed(ws, A, ".accepted", None)
    accepted = ws / "deliveries" / A / "task-r.accepted"
    started = threading.Event()

    def releaser() -> None:
        started.wait()
        pd.release(accepted)                      # takes A's folder lock, renames .accepted -> .txt

    th = threading.Thread(target=releaser, daemon=True)
    th.start()

    def between(name: Path, state: str) -> None:
        if name.name.endswith(".txt") and not started.is_set():
            started.set()                         # the releaser now wants A's lock
            time.sleep(0.7)                       # long enough for it to run if the lock did not hold it
    try:
        out = rt.route(ws, task(ws), _between_suffix_checks=between)
    except TypeError as e:               # a head without the serialised holder read
        started.set()
        th.join(5)
        check("the replay reads each folder's suffix set under its lock", False, str(e))
        return
    th.join(5)
    check("the replay saw A's delivery under one of its names and reported already for A",
          out["already"] == [A] and out["delivered"] == [], str(out))
    check("no sentinel was created for B", not (ws / "deliveries" / B / "task-r.txt").exists(), str(sentinels(ws)))
    check("the release completed afterwards", (ws / "deliveries" / A / "task-r.txt").exists())


def scenario_aliased_recipient_directory_refuses() -> None:
    print("\nscenario: deliveries/A is a symlink to a directory outside deliveries/ holding task-r.txt")
    ws = workspace(B)
    outside = ws.parent / "outside"
    outside.mkdir()
    (outside / "task-r.txt").touch()
    (ws / "deliveries" / A).symlink_to(outside)
    try:
        rt.route(ws, task(ws))
        check("aliased recipient directory: refused", False, "route returned")
    except rt.UnreadableEvidence as e:
        check("aliased recipient directory: refused and named", "recipient folders" in str(e) or "alias" in str(e).lower(), str(e))
    check("no attribution was written", pa.worker_for_task(ws, "task-r") is None)
    check("no lock was created outside deliveries/", not (outside / ".lock").exists())
    check("no sentinel for B", not (ws / "deliveries" / B / "task-r.txt").exists())
    # The lock owner itself refuses an alias, so a swap after enumeration cannot gain a lock.
    try:
        with pd.arbitration(ws, A):
            check("folder arbitration on an aliased recipient: refused", False, "lock taken")
    except OSError as e:
        check("folder arbitration on an aliased recipient: refused by the lock owner", "alias" in str(e), str(e))
    check("...and still no lock outside deliveries/", not (outside / ".lock").exists())


def scenario_unlistable_root_and_unreadable_record() -> None:
    print("\nscenario: deliveries/ itself unlistable; the attribution record unreadable")
    ws = workspace(B)
    seed(ws, A, ".txt", None)
    (ws / "deliveries").chmod(0o000)
    try:
        try:
            rt.route(ws, task(ws))
            check("unlistable deliveries/: refused", False, "route returned")
        except rt.UnreadableEvidence as e:
            check("unlistable deliveries/: refused and named", "cannot list" in str(e) or "cannot read the recipient folders" in str(e), str(e))
    finally:
        (ws / "deliveries").chmod(0o755)
    ws = workspace(B)
    seed(ws, A, ".txt", A)
    pa.attribution_path(ws, "task-r").chmod(0o000)
    try:
        try:
            rt.route(ws, task(ws))
            check("unreadable attribution: refused", False, "route returned")
        except rt.UnreadableEvidence as e:
            check("unreadable attribution: refused and named", "attribution record unreadable" in str(e), str(e))
    finally:
        pa.attribution_path(ws, "task-r").chmod(0o644)
    check("no sentinel for B in either case", not (ws / "deliveries" / B / "task-r.txt").exists())
    ws = workspace(A)
    calls = []
    out = rt.deliver_one(ws, A, "task-r", _between_sentinel_and_attribution=lambda: calls.append(1))
    check("the seam runs once inside deliver_one, between the sentinel and the attribution",
          out == "delivered" and calls == [1] and pa.worker_for_task(ws, "task-r") == A, str((out, calls)))


def snapshot(d: Path) -> dict:
    return {str(q.relative_to(d)): (q.stat().st_size, q.stat().st_mtime_ns) for q in sorted(d.rglob("*"))}


def swap_to_alias(ws: Path, outside: Path) -> Path:
    """Replace deliveries/A (a real directory) with a symlink to `outside`; the real one is moved aside."""
    real = ws / "deliveries" / A
    moved = ws / "deliveries" / (A + ".moved")
    os.rename(real, moved)
    real.symlink_to(outside)
    return moved


def scenario_swap_before_open_refuses_without_touching_outside() -> None:
    print("\nscenario: deliveries/A is swapped for A -> outside AFTER ownership validation, BEFORE the directory open")
    ws = workspace(B)
    seed(ws, A, ".txt", None)
    outside = ws.parent / "outside"
    outside.mkdir()
    (outside / "task-r.txt").touch()
    before = snapshot(outside)
    seams = {"_between_validation_and_open": lambda: swap_to_alias(ws, outside)}
    try:
        rt.route(ws, task(ws), _arbitration_seams=seams)
        check("pre-open swap: refused", False, "route returned")
    except (rt.RouterRefused, TypeError) as e:
        check("pre-open swap: refused by the lock owner (no-follow directory open)", isinstance(e, rt.RouterRefused), str(e))
    check("no outside/.lock was created and outside is byte-for-byte untouched",
          not (outside / ".lock").exists() and snapshot(outside) == before, str(sorted(p.name for p in outside.iterdir())))
    check("no sentinel for B", not (ws / "deliveries" / B / "task-r.txt").exists())


def scenario_swap_after_flock_body_stays_anchored() -> None:
    print("\nscenario: deliveries/A is swapped for A -> outside AFTER the flock, BEFORE the first suffix probe (read/create path)")
    ws = workspace(A)                                # bound to A: this pass will CREATE A's sentinel
    (ws / "deliveries" / A).mkdir(parents=True, exist_ok=True)
    outside = ws.parent / "outside"
    outside.mkdir()
    before = snapshot(outside)
    moved = {}
    seams = {"_after_lock": lambda: moved.setdefault("dir", swap_to_alias(ws, outside))}
    try:
        out = rt.deliver_one(ws, A, "task-r", _arbitration_seams=seams)   # the create, on its own lock
    except TypeError as e:
        check("post-flock swap (read/create): the body is anchored to the directory fd", False, str(e))
        return
    check("the delivery was made", out == "delivered", str(out))
    check("...into the ORIGINAL directory, through the fd", (moved["dir"] / "task-r.txt").exists(), str(sorted(p.name for p in moved["dir"].iterdir())))
    # A swap that lands between holders' lock and the create's lock is a refusal at the next lock, never a create through the alias.
    import pool_route_handler as h
    ws2 = workspace(A)
    (ws2 / "deliveries" / A).mkdir(parents=True, exist_ok=True)
    outside2 = ws2.parent / "outside"
    outside2.mkdir()
    before2 = snapshot(outside2)
    moved2 = {}
    real_route = rt.route
    rt.route = lambda w_, t_, roster=None, **kw: real_route(w_, t_, roster, _arbitration_seams={"_after_lock": lambda: moved2.setdefault("d", swap_to_alias(ws2, outside2))})
    try:
        rc = h.main(["--task-file", str(ws2 / "tasks" / "task-r.txt"), "--workspace", str(ws2)])
    finally:
        rt.route = real_route
    check("the handler answers must-handle when the alias appears between the holder read and the create", rc == h.MUST_HANDLE, str(rc))
    check("...and nothing was created through the alias", snapshot(outside2) == before2 and not (outside2 / "task-r.txt").exists())
    check("outside is byte-for-byte untouched (nothing read or created through the alias)",
          snapshot(outside) == before and not (outside / "task-r.txt").exists() and not (outside / ".lock").exists(),
          str(sorted(p.name for p in outside.iterdir())))
    # And the rename/unlink path: release() of an accepted sentinel, swapped after its flock.
    ws = workspace(A)
    seed(ws, A, ".accepted", None)
    outside = ws.parent / "outside"
    outside.mkdir()
    (outside / "task-r.accepted").touch()
    before = snapshot(outside)
    real_arb = pd.arbitration

    @__import__("contextlib").contextmanager
    def swapping_arb(workspace, recipient, **kw):
        with real_arb(workspace, recipient, _after_lock=lambda: moved.setdefault("rel", swap_to_alias(ws, outside))) as dfd:
            yield dfd
    moved.clear()
    pd.arbitration = swapping_arb
    try:
        dst = pd.release(ws / "deliveries" / A / "task-r.accepted")
    finally:
        pd.arbitration = real_arb
    check("release renamed within the ORIGINAL directory, through the fd",
          (moved["rel"] / "task-r.txt").exists() and not (moved["rel"] / "task-r.accepted").exists(), str(sorted(p.name for p in moved["rel"].iterdir())))
    check("outside is byte-for-byte untouched by the rename path", snapshot(outside) == before, str(sorted(p.name for p in outside.iterdir())))
    # clear_pending's unlink path, the same way.
    ws = workspace(A)
    seed(ws, A, ".txt", None)
    outside = ws.parent / "outside"
    outside.mkdir()
    (outside / "task-r.txt").touch()
    before = snapshot(outside)
    moved.clear()
    pd.arbitration = swapping_arb
    try:
        pd.clear_pending(ws, A, "task-r")
    finally:
        pd.arbitration = real_arb
    check("clear_pending unlinked within the ORIGINAL directory, through the fd",
          "rel" in moved and not (moved["rel"] / "task-r.txt").exists(), str(sorted(p.name for p in moved.get("rel", outside).iterdir())))
    check("outside is byte-for-byte untouched by the unlink path", snapshot(outside) == before and (outside / "task-r.txt").exists())


def scenario_every_arbitration_body_is_fd_relative() -> None:
    print("\nscenario: structural pin — no path-based probe, create, rename or unlink inside any arbitration body")
    import ast
    offenders = []
    for mod in ("pool_delivery.py", "pool_router.py"):
        tree = ast.parse((SCRIPTS / mod).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any("arbitration" in ast.unparse(i.context_expr) for i in node.items):
                body_src = "\n".join(ast.unparse(b) for b in node.body)
                for bad in ("os.rename(sentinel,", "os.rename(src,", "os.unlink(sentinel)", ".exists()", "pd.find(", " find(", "os.open(d /", "os.open(str("):
                    if bad in body_src:
                        offenders.append(f"{mod}: {bad!r} in a with-arbitration body at line {node.lineno}")
    check("every arbitration body reads and mutates through the directory fd", not offenders, "; ".join(offenders))


def main() -> int:
    scenario_plain_replay_is_still_already()
    scenario_swap_before_open_refuses_without_touching_outside()
    scenario_swap_after_flock_body_stays_anchored()
    scenario_every_arbitration_body_is_fd_relative()
    scenario_unreadable_and_malformed_evidence_refuse()
    scenario_commit_outlives_the_roster()
    scenario_handler_settles_by_the_routes_targets()
    scenario_release_race_is_serialised_by_the_folder_lock()
    scenario_aliased_recipient_directory_refuses()
    scenario_unlistable_root_and_unreadable_record()
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
