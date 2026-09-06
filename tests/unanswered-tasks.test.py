#!/usr/bin/env python3
"""The unanswered-task check must FIRE on a missing result and STAY QUIET on
every shape a delivered result actually takes.

A checker that cannot fire is the failure this guards against, so the first
assertion is that the positive case is reachable at all.
Run: python3 tests/unanswered-tasks.test.py
"""
import importlib.util
import sys
import tempfile
import time
from pathlib import Path

_s = importlib.util.spec_from_file_location(
    "uat", str(Path(__file__).resolve().parent.parent / "scripts" / "unanswered-tasks.py"))
uat = importlib.util.module_from_spec(_s)
_s.loader.exec_module(uat)

PASS = FAIL = 0


def check(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def ws(tmp, task_id="task-abc123", result=None, age_sec=600):
    root = Path(tmp)
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(parents=True, exist_ok=True)
    t = root / "tasks" / f"{task_id}.txt"
    t.write_text("id: x\n")
    old = time.time() - age_sec
    import os
    os.utime(t, (old, old))
    if result:
        p = root / "results" / result
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("done\n")
    return root


with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(ws(d), min_age_sec=120)
    check([r[0] for r in rows] == ["task-abc123"], "FIRES on a task with no result (the case this exists for)")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(ws(d, result="task-abc123.txt"), 120) == [], "quiet: plain results/<id>.txt")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(ws(d, result="phone-CA123.task-abc123.txt"), 120) == [],
          "quiet: per-channel pull namespace <key>.task-<id>.txt")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(ws(d, result="archive/2026-09/task-abc123-1788375000.txt"), 120) == [],
          "quiet: archived task-<id>-<epoch>.txt under archive/YYYY-MM/")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(ws(d, age_sec=5), 120) == [], "quiet: task younger than min-age is still in flight")

with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "tasks").mkdir(parents=True)
    check(uat.unanswered(root, 120) == [], "no results/ dir is not a crash")

check(uat.unanswered(Path("/nonexistent-xyz"), 120) == [], "absent workspace is empty, not an error")

# A bridge claims a proactive result by rename; that is delivery in flight, not a miss.
with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(ws(d, result="task-abc123.txt.sending"), 120) == [],
          "quiet: a `.sending` claim is mid-delivery, not unanswered")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(ws(d, result="discord-1.task-abc123.txt.sending"), 120) == [],
          "quiet: a scoped `.sending` claim too")


# --- main(): the exit code is the whole contract for a pass-closing check ------
def run_main(argv):
    """main() with argv patched; returns (rc, stdout)."""
    import contextlib
    import io
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["unanswered-tasks.py"] + argv
    try:
        with contextlib.redirect_stdout(buf):
            rc = uat.main()
    finally:
        sys.argv = old_argv
    return rc, buf.getvalue()


with tempfile.TemporaryDirectory() as d:
    rc, out = run_main(["--workspace", str(ws(d))])
    check(rc == 1, "main() EXITS 1 when a task has no result (what the loop keys on)")
    check("task-abc123" in out, "main() names the offending task, not just a count")
    check("the room heard nothing" in out, "main() says what the miss cost")

with tempfile.TemporaryDirectory() as d:
    rc, out = run_main(["--workspace", str(ws(d, result="task-abc123.txt"))])
    check(rc == 0, "main() exits 0 when every task is answered")
    check(out.strip() == "unanswered-tasks: none", "main() is quiet-but-explicit on the clean path")

with tempfile.TemporaryDirectory() as d:
    # --min-age-sec is the in-flight guard; prove it is honoured through the CLI.
    rc, _ = run_main(["--workspace", str(ws(d, age_sec=600)), "--min-age-sec", "99999"])
    check(rc == 0, "main() honours --min-age-sec (a young task is not a miss)")


# --- [deduped: X] is only an answer if X answered ----------------------------
# A marker naming a task with no result leaves a result file on disk either way.
def dedup_case(tmp, *, target_exists, chain=False, cycle=False):
    import os
    root = Path(tmp)
    (root / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(parents=True, exist_ok=True)
    t = root / "tasks" / "task-src.txt"
    t.write_text("id: x\n")
    old = time.time() - 600
    os.utime(t, (old, old))
    res = root / "results"
    if cycle:
        (res / "task-src.txt").write_text("[deduped: task-mid]\n")
        (res / "task-mid.txt").write_text("[deduped: task-src]\n")
        return root
    hops = ["task-mid", "task-dst"] if chain else ["task-dst"]
    (res / "task-src.txt").write_text(f"[deduped: {hops[0]}]\n")
    for a, b in zip(hops, hops[1:]):
        (res / f"{a}.txt").write_text(f"[deduped: {b}]\n")
    if target_exists:
        (res / f"{hops[-1]}.txt").write_text("the actual reply\n")
    return root


with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(dedup_case(d, target_exists=False), 120)
    check(len(rows) == 1 and "task-dst" in rows[0][2],
          "FIRES when [deduped: X] points at a task with no result (the loss this closes)")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(dedup_case(d, target_exists=True), 120) == [],
          "quiet when [deduped: X] points at a task that DID answer")

with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(dedup_case(d, target_exists=True, chain=True), 120)
    check(len(rows) == 1 and rows[0][2].startswith("HOLDER-SKIPPED"),
          "a CHAIN is flagged: the bridge never delivers through a dedup holder")

with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(dedup_case(d, target_exists=False, cycle=True), 120)
    check(len(rows) == 1 and rows[0][2].startswith("HOLDER-SKIPPED"),
          "a dedup CYCLE is flagged (single-hop, so recursion is impossible by construction)")


# A DELIVERED target archives as `task-<id>-<epoch>.txt`, never `<id>.txt`.
# An exact-name predicate would call every delivered target ORPHANED.
with tempfile.TemporaryDirectory() as d:
    import os
    root = Path(d)
    (root / "tasks").mkdir(parents=True); (root / "results" / "archive" / "2026-09").mkdir(parents=True)
    t = root / "tasks" / "task-src.txt"; t.write_text("id: x\n")
    old = time.time() - 600; os.utime(t, (old, old))
    (root / "results" / "task-src.txt").write_text("[deduped: task-dst]\n")
    (root / "results" / "archive" / "2026-09" / "task-dst-1788376009.txt").write_text("the actual reply\n")
    check(uat.unanswered(root, 120) == [],
          "quiet when the dedup target was DELIVERED (archived with an epoch suffix)")


# A prefix without a separator also matches `{id}.too-old.<epoch>` (QUARANTINED)
# and any longer id sharing the prefix — both would read as delivered.
with tempfile.TemporaryDirectory() as d:
    import os
    root = Path(d)
    (root / "tasks").mkdir(parents=True); (root / "results" / "archive" / "2026-09").mkdir(parents=True)
    t = root / "tasks" / "task-src.txt"; t.write_text("id: x\n")
    old = time.time() - 600; os.utime(t, (old, old))
    (root / "results" / "task-src.txt").write_text("[deduped: task-dst]\n")
    a = root / "results" / "archive" / "2026-09"
    (a / "task-dst.too-old.1787014338.txt").write_text("aged out, never delivered\n")
    (a / "task-dstrework-1788376009.txt").write_text("a DIFFERENT task sharing the prefix\n")
    rows = uat.unanswered(root, 120)
    check(len(rows) == 1 and "task-dst" in rows[0][2],
          "a QUARANTINED (.too-old) target does not count as delivered")


# DANGLING (target never existed here) and ORPHANED (own target never answered)
# need opposite fixes, so a checker that reports one label for both misroutes.
def split_case(tmp, *, target_task_exists):
    import os
    root = Path(tmp)
    (root / "tasks" / "archive").mkdir(parents=True); (root / "results").mkdir(parents=True)
    t = root / "tasks" / "task-src.txt"; t.write_text("id: x\n")
    old = time.time() - 600; os.utime(t, (old, old))
    (root / "results" / "task-src.txt").write_text("[deduped: task-dst]\n")
    if target_task_exists:
        (root / "tasks" / "archive" / "task-dst.txt").write_text("id: dst\n")
    return root


with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(split_case(d, target_task_exists=False), 120)
    check(len(rows) == 1 and rows[0][2].startswith("DANGLING"),
          "a target that never existed here is DANGLING (a peer's id), not ORPHANED")

with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(split_case(d, target_task_exists=True), 120)
    check(len(rows) == 1 and rows[0][2].startswith("ORPHANED"),
          "a target that exists here but never answered is ORPHANED, not DANGLING")


# Resolving is not reaching. The target ANSWERED, so every existence check
# passes -- and the reply went to a different room, silencing this sender.
def cross_room_case(tmp, *, same_channel):
    import os
    root = Path(tmp)
    (root / "tasks" / "archive").mkdir(parents=True); (root / "results").mkdir(parents=True)
    t = root / "tasks" / "task-src.txt"
    t.write_text("id: src\nchannel_id: !roomA:ag2.space\n")
    old = time.time() - 600; os.utime(t, (old, old))
    dest = "!roomA:ag2.space" if same_channel else "!roomB:ag2.space"
    (root / "tasks" / "archive" / "task-dst.txt").write_text(f"id: dst\nchannel_id: {dest}\n")
    (root / "results" / "task-src.txt").write_text("[deduped: task-dst]\n")
    (root / "results" / "task-dst.txt").write_text("the reply, delivered to task-dst's room\n")
    return root


with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(cross_room_case(d, same_channel=False), 120)
    check(len(rows) == 1 and rows[0][2].startswith("CROSS-ROOM"),
          "FIRES on a dedup whose target answers a DIFFERENT room (the target did answer)")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(cross_room_case(d, same_channel=True), 120) == [],
          "quiet when the dedup target is the SAME room and sender (what dedup is for)")


# A [no-send] / [REPLIED] holder never delivered, so the bridge requeues. This
# is `dedup_holder_delivered`'s verdict, not a rule restated here.
for marker in ("[no-send]", "[REPLIED]"):
    with tempfile.TemporaryDirectory() as d:
        import os
        root = Path(d)
        (root / "tasks" / "archive").mkdir(parents=True); (root / "results").mkdir(parents=True)
        t = root / "tasks" / "task-src.txt"; t.write_text("id: src\n")
        old = time.time() - 600; os.utime(t, (old, old))
        (root / "tasks" / "archive" / "task-dst.txt").write_text("id: dst\n")
        (root / "results" / "task-src.txt").write_text("[deduped: task-dst]\n")
        (root / "results" / "task-dst.txt").write_text(marker + "\n")
        rows = uat.unanswered(root, 120)
        check(len(rows) == 1 and rows[0][2].startswith("HOLDER-SKIPPED"),
              f"a {marker} holder is not a delivery — flagged, as the bridge requeues it")


# The guard must fire on an EMPTY queue too: a lazy per-task import means a
# missing result_markers.py reports "none" instead of refusing.
import shutil
import subprocess

_REPO = Path(__file__).resolve().parent.parent


def _fake_repo(d: Path, with_soundness: bool):
    """A repo holding unanswered-tasks.py and, optionally, its judgement owner —
    but never result_markers.py, so the grammar owner is the missing one."""
    (d / "src").mkdir(); (d / "scripts").mkdir()
    shutil.copy(_REPO / "scripts" / "unanswered-tasks.py", d / "scripts" / "unanswered-tasks.py")
    if with_soundness:
        shutil.copy(_REPO / "src" / "dedup_soundness.py", d / "src" / "dedup_soundness.py")
    ws = d / "ws"; (ws / "tasks").mkdir(parents=True); (ws / "results").mkdir()
    return subprocess.run([sys.executable, str(d / "scripts" / "unanswered-tasks.py"),
                           "--workspace", str(ws)], capture_output=True, text=True, cwd=d)


with tempfile.TemporaryDirectory() as d:
    proc = _fake_repo(Path(d), with_soundness=True)
    check(proc.returncode == 2 and "result_markers" in proc.stderr,
          "refuses (exit 2) when result_markers.py is unimportable, even with an empty queue")

with tempfile.TemporaryDirectory() as d:
    # Exit 2, not 1: the loop's checkers reserve 1 for a real finding, so a
    # broken tool exiting 1 reads as damage to the user's data.
    proc = _fake_repo(Path(d), with_soundness=False)
    check(proc.returncode == 2 and "dedup_soundness" in proc.stderr,
          "refuses (exit 2) when src/dedup_soundness.py is unimportable")


# The real shape: ONE shared room, many senders. Comparing channels is silent
# here, which is where every measured cross-sender silence actually happened.
def same_room_case(tmp, *, same_sender):
    import os
    root = Path(tmp)
    (root / "tasks" / "archive").mkdir(parents=True); (root / "results").mkdir(parents=True)
    t = root / "tasks" / "task-src.txt"
    t.write_text("id: src\nchannel_id: !room:ag2.space\nuser_id: @alice:ag2.space\n")
    old = time.time() - 600; os.utime(t, (old, old))
    who = "@alice:ag2.space" if same_sender else "@bob:ag2.space"
    (root / "tasks" / "archive" / "task-dst.txt").write_text(
        f"id: dst\nchannel_id: !room:ag2.space\nuser_id: {who}\n")
    (root / "results" / "task-src.txt").write_text("[deduped: task-dst]\n")
    (root / "results" / "task-dst.txt").write_text("the reply, delivered to dst's sender\n")
    return root


with tempfile.TemporaryDirectory() as d:
    rows = uat.unanswered(same_room_case(d, same_sender=False), 120)
    check(len(rows) == 1 and rows[0][2].startswith("CROSS-SENDER"),
          "FIRES on same-room different-sender — the shape a channel compare cannot see")

with tempfile.TemporaryDirectory() as d:
    check(uat.unanswered(same_room_case(d, same_sender=True), 120) == [],
          "quiet on same-room same-sender — the legitimate dedup")

# Error and edge paths. The refusal is asserted above too, but via subprocess,
# where no tracer follows — so these drive the same branches in-process.

# These now live in src/dedup_soundness.py, shared with the PRE-write guard, so
# the assertions follow the code to its owner rather than through a re-export.
_ds = uat.dedup_soundness

with tempfile.TemporaryDirectory() as d:
    root = Path(d); (root / "tasks").mkdir()
    (root / "tasks" / "task-live.txt").write_text("id: live\n")
    check(_ds.task_exists(root / "tasks", "task-live") is True,
          "task_exists: True while the task is still LIVE in tasks/ (not yet archived)")
    check(_ds.task_exists(root / "tasks", "task-never") is False,
          "task_exists: False for an id that never existed here")

with tempfile.TemporaryDirectory() as d:
    check(uat._read(Path(d)) is None, "_read: a directory (OSError) reads as None rather than raising")
    check(uat._read(None) is None, "_read: a None path stays None")

with tempfile.TemporaryDirectory() as d:
    root = Path(d); (root / "results").mkdir()
    (root / "results" / "task-x.txt").write_text("[deduped: ]\n")
    check(uat._unanswered_reason(root / "results", "task-x") == "deduped into nothing (no target id)",
          "a dedup naming NO target is unanswered — the marker parsed, the reply went nowhere")

# `sys.modules[name] = None` is what makes `from name import x` raise ImportError,
# so the refusal runs in-process instead of in a child the tracer cannot see.
import contextlib
import io
_saved_markers, _had_mod = _ds._MARKERS, "result_markers" in sys.modules
_saved_mod = sys.modules.get("result_markers")
_ds._MARKERS = None
sys.modules["result_markers"] = None
_err = io.StringIO()
try:
    with contextlib.redirect_stderr(_err):
        uat._markers()
    check(False, "_markers refuses when src/result_markers.py is unimportable")
except SystemExit as exc:
    check(exc.code == 2, "_markers exits 2 when src/result_markers.py is unimportable")
    check("result_markers" in _err.getvalue() and "re-implement" in _err.getvalue(),
          "the refusal names the missing module and says it will not re-implement the grammar")
finally:
    if _had_mod:
        sys.modules["result_markers"] = _saved_mod
    else:
        sys.modules.pop("result_markers", None)
    _ds._MARKERS = _saved_markers

check(uat._markers() is not None,
      "CONTROL: _markers still works afterwards — the refusal test restored global state")


print(f"\nunanswered-tasks: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
