#!/usr/bin/env python3
"""A queued task held by a running worker is not a stalled queue.

A delegated team task stays in `tasks/` for its whole run — the worker removes
it only when it publishes a result — so an in-flight task and an abandoned one
are byte-identical on disk. The probe warned "watcher or core may be stuck" on
both, which fires on ordinary operation and trains the reader to ignore it.

The suppression is BOUNDED by the worker's own hard deadline. Past
SUTANDO_TIER_HARD_TIMEOUT (session-worker.py:249, default 900s) a live holder
has outlived the limit it enforces on itself, so "held" stops meaning "working"
and starts meaning "wedged" — and that is precisely when an unbounded version
of this probe would go quiet forever.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


PS = """\
/bin/bash src/watch-tasks-stream.sh --handler-runner /r/skills/task-workstream-sessions/scripts/session-worker.py claude /w /w/tasks/task-aaa.txt /w/results /r /tmp/ev task-aaa.txt
/usr/bin/python3 /r/skills/task-workstream-sessions/scripts/session-worker.py --runtime claude --workspace /w --task-file /w/tasks/task-bbb.txt --results-dir /w/results --repo /r
/usr/bin/python3 src/health-check.py
/bin/bash src/watch-tasks-stream.sh
"""

held = hc._tasks_held_by_a_worker(PS)
check(held == {"task-aaa.txt", "task-bbb.txt"},
      f"both worker argv shapes are recognised (got {sorted(held)})")
check("task-aaa.txt" in held, "the --handler-runner wrapper form is seen")
check("task-bbb.txt" in held, "the --task-file form is seen")

# The plain watcher and the health-check itself must not register as workers.
check(hc._tasks_held_by_a_worker(
    "/bin/bash src/watch-tasks-stream.sh\n/usr/bin/python3 src/health-check.py\n") == set(),
    "a bare watcher and health-check itself hold nothing")

# Only paths under tasks/ count — a results path in the same argv is not a claim.
check(hc._tasks_held_by_a_worker(
    "python3 session-worker.py --task-file /w/results/task-ccc.txt\n") == set(),
    "a non-tasks/ path is not counted as a held task")

# Degrade to empty rather than raising: a probe must never fail the check.
check(hc._tasks_held_by_a_worker("") == set(), "empty ps output yields an empty set")
check(hc._tasks_held_by_a_worker("garbage\n\n   \n") == set(), "junk ps output does not raise")

# The wording is the point: "may be stuck" must not appear when every queued
# task is accounted for by a live worker.
src = (REPO / "src" / "health-check.py").read_text()
i = src.index("def check_task_queue")
# The function itself, not a byte count: a fixed window turns "someone added a
# line" into a false failure on an assertion about wording.
body = src[i:src.index("\ndef ", i + 1)]
check("held_note" in body, "the probe threads the in-flight count into its detail")
check("held_is_progress" in body,
      "suppression is gated on a single named predicate, not an inline comparison")
check("not stalled" in body, "the all-held case says so explicitly")

# BEHAVIOURAL: a reworded detail with status="warn" still alerts, which no
# wording assertion can see. These call the probe and read what the notifier does.
import os
import tempfile
import time as _time

ALERTABLE = ("down", "missing", "not_loaded", "fail", "stale", "warn")


def probe(n_tasks, n_held, age_sec, real_lookup=False, worker_age=None):
    """Run the real check_task_queue against a temp workspace.

    age_sec is the TASK FILE's age; worker_age is the holder's RUNTIME. They are
    independent by construction, which is the whole point — a queued task can be
    old while the worker that just claimed it has run for seconds. worker_age
    defaults to age_sec so the pre-existing cases keep their original meaning.

    real_lookup keeps the shipped worker lookup so a broken `ps` is exercised.
    """
    tmp = Path(tempfile.mkdtemp())
    (tmp / "tasks").mkdir()
    names = []
    for i in range(n_tasks):
        f = tmp / "tasks" / f"task-{i:03d}.txt"
        f.write_text("id: x\n")
        old_t = _time.time() - age_sec
        os.utime(f, (old_t, old_t))
        names.append(f.name)
    ages = age_sec if worker_age is None else worker_age
    holdings = {n: ages for n in names[:n_held]}
    # Stub whichever lookup the module has, so this file also runs against a
    # merge-base that predates _worker_holdings and FAILs rather than raising.
    attr = "_worker_holdings" if hasattr(hc, "_worker_holdings") else "_tasks_held_by_a_worker"
    stub = (lambda ps_output=None: holdings) if attr == "_worker_holdings" \
        else (lambda ps_output=None: set(holdings))
    orig_ws, orig_hold = hc.WORKSPACE_DIR, getattr(hc, attr)
    hc.WORKSPACE_DIR = tmp
    if not real_lookup:
        setattr(hc, attr, stub)
    try:
        return hc.check_task_queue()
    finally:
        hc.WORKSPACE_DIR = orig_ws
        setattr(hc, attr, orig_hold)

# The ordinary case this PR exists for: held, and still inside the deadline.
r = probe(1, 1, 400)
check(r["status"] == "ok", f"held + UNDER deadline -> ok (got {r['status']!r})")
check(r["status"] not in ALERTABLE,
      "all-held does NOT reach emit_task_for_failures / notify_for_failures")
# One task under both thresholds reaches neither branch, so no wording claim
# here; it is asserted below on the count+age branch, the only place it survives.

# THE BOUND: 1000s is past the worker's own hard deadline (default 900), so
# "a live worker holds it" is no longer evidence that anything is progressing.
r = probe(1, 1, 1000)
check(r["status"] == "warn", f"held + PAST deadline -> warn (got {r['status']!r})")
check(r["status"] in ALERTABLE, "a wedged worker reaches the notifier")
check("WORKER is wedged" in r["detail"],
      "the detail points at the worker, not the watcher")
check("not stalled" not in r["detail"],
      "and it drops the reassurance it can no longer support")

# A genuinely stalled queue must be unchanged — this is the probe's real job.
r = probe(1, 0, 1000)
check(r["status"] == "warn", f"unheld + past stuck age -> warn (got {r['status']!r})")
check(r["status"] in ALERTABLE, "a real stall still alerts")

# Mixed: partial accounting must NOT buy silence.
r = probe(4, 2, 400)
check(r["status"] == "warn", f"mixed queue -> warn (got {r['status']!r})")
check("2 in flight with a worker" in r["detail"], "mixed reports how many are accounted for")

# Count+age branch, fully held, inside the deadline.
r = probe(4, 4, 400)
check(r["status"] == "ok", f"count+age branch, all held -> ok (got {r['status']!r})")
check("not stalled" in r["detail"], "the detail still explains why it is quiet")

# The count+age branch carries the SAME suppression and returns FIRST, so
# bounding only stuck_age_sec would leave a 4-task all-held pile quiet at any age.
r = probe(4, 4, 1000)
check(r["status"] == "warn",
      f"count+age branch, all held PAST deadline -> warn (got {r['status']!r})")
check("WORKER is wedged" in r["detail"],
      "count+age branch also names the worker once past the deadline")

# A broken `ps` is two questions, not one: does it stay quiet, and does the
# silence it buys disable the probe? Both are asserted; the second is the point.
def _raising_run(*_a, **_kw):
    raise OSError("ps unavailable")

_orig_run = hc.subprocess.run
hc.subprocess.run = _raising_run
try:
    check(hc._tasks_held_by_a_worker() == set(),
          "a `ps` that raises yields no held set instead of propagating")
    r = probe(1, 0, 1000, real_lookup=True)
    check(r["status"] == "warn",
          f"with `ps` broken a real stall STILL warns (got {r['status']!r})")
finally:
    hc.subprocess.run = _orig_run

# ---- the bound is the WORKER's CONFIGURED deadline, not a constant -----------
# A fixed 900s pages above a raised deadline and hides an overdue worker below it.
import contextlib


@contextlib.contextmanager
def hard_timeout(value):
    prev = os.environ.get("SUTANDO_TIER_HARD_TIMEOUT")
    if value is None:
        os.environ.pop("SUTANDO_TIER_HARD_TIMEOUT", None)
    else:
        os.environ["SUTANDO_TIER_HARD_TIMEOUT"] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("SUTANDO_TIER_HARD_TIMEOUT", None)
        else:
            os.environ["SUTANDO_TIER_HARD_TIMEOUT"] = prev


# The two BEHAVIOURAL controls come first and use nothing this change adds, so
# they fail as FAILs at the merge-base instead of dying on an AttributeError.
with hard_timeout("1800"):
    r = probe(1, 1, 1000)
    check(r["status"] == "ok",
          f"configured 1800s, held 1000s -> ok (got {r['status']!r})")
    check("not stalled" in r["detail"], "and it says the worker is still working")
    check("900s" not in r["detail"], "the detail quotes the CONFIGURED deadline, not 900")

# Age 400s is UNDER stuck_age_sec, so the age test alone can never reach this.
with hard_timeout("300"):
    r = probe(1, 1, 400)
    check(r["status"] == "warn",
          f"configured 300s, held 400s -> warn (got {r['status']!r})")
    check(r["status"] in ALERTABLE, "an overdue worker reaches the notifier")
    check("300s hard deadline" in r["detail"], "the detail names the configured deadline")

# The new clause is gated on all_held: an UNHELD young queue must stay quiet, or
# every ordinary short-deadline deploy warns on a task nobody has claimed yet.
with hard_timeout("300"):
    r = probe(1, 0, 400)
    check(r["status"] == "ok",
          f"unheld + under stuck age stays ok on a short deadline (got {r['status']!r})")
    r = probe(4, 2, 400)
    check(r["status"] == "warn", f"a mixed queue still warns (got {r['status']!r})")

check(hasattr(hc, "_worker_hard_timeout_s"),
      "one named resolver owns the deadline, so claim-age and queue cannot drift")
if hasattr(hc, "_worker_hard_timeout_s"):
    with hard_timeout(None):
        check(hc._worker_hard_timeout_s() == 900.0, "unset resolves to the worker's own default")
    with hard_timeout("1800"):
        check(hc._worker_hard_timeout_s() == 1800.0, "a configured longer deadline is honoured")
    with hard_timeout("300"):
        check(hc._worker_hard_timeout_s() == 300.0, "a configured shorter deadline is honoured")
    # Non-positive and unparseable both make the worker refuse to start, so no
    # running worker can hold them; the fallback must never page a permitted run.
    for bad in ("0", "-5", "", "abc", "None"):
        with hard_timeout(bad):
            check(hc._worker_hard_timeout_s() == 900.0,
                  f"{bad!r} falls back to the default rather than to a pageable bound")

# The claim-age probe reads the SAME resolver, so the two cannot drift apart.
with hard_timeout("7200"):
    check(hc._task_claim_thresholds() == (14400.0, 57600.0),
          f"claim thresholds track the configured deadline (got {hc._task_claim_thresholds()})")

# ---- the deadline is the WORKER's RUNTIME, not the task file's age ------------
# A queued task ages while no worker is running; file age is a different question.

# BEHAVIOURAL first, using only probe(), so they FAIL at the merge-base.
with hard_timeout("900"):
    r = probe(1, 1, 1000, worker_age=5)
    check(r["status"] == "ok",
          f"file 1000s old, worker running 5s -> ok (got {r['status']!r})")
    check("wedged" not in r["detail"], "and it is not called wedged")

# The reciprocal. At the merge-base it passes for the WRONG reason (file age),
# so the PAIR discriminates, not either half alone.
with hard_timeout("900"):
    r = probe(1, 1, 1200, worker_age=1200)
    check(r["status"] == "warn",
          f"worker running 1200s past a 900s deadline -> warn (got {r['status']!r})")
    check("running 1200s" in r["detail"],
          f"the detail quotes the WORKER's runtime (got {r['detail'][-70:]!r})")

check(hasattr(hc, "_worker_holdings"),
      "a named helper returns holder RUNTIME, so file age and worker age cannot be confused")
check(hasattr(hc, "_parse_etime"), "ps ELAPSED has a parser of its own")

if hasattr(hc, "_parse_etime"):
    check(hc._parse_etime("00:42") == 42, "etime MM:SS")
    check(hc._parse_etime("01:00:00") == 3600, "etime HH:MM:SS")
    check(hc._parse_etime("2-03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5, "etime D-HH:MM:SS")
    for bad in ("", "   ", "abc", "1:2:3:4", "x-01:00", "1:aa"):
        check(hc._parse_etime(bad) is None, f"etime {bad!r} is unreadable, not zero")

if hasattr(hc, "_worker_holdings"):
    PS_AGED = ("  900:01 /usr/bin/python3 /r/skills/task-workstream-sessions/scripts/"
               "session-worker.py --task-file /w/tasks/task-old.txt\n")
    PS_FRESH = ("    00:03 /usr/bin/python3 /r/skills/task-workstream-sessions/scripts/"
                "session-worker.py --task-file /w/tasks/task-new.txt\n")
    # SUPERSEDED semantics, asserted rather than deleted: past the deadline,
    # "waiting" can no longer explain an absent mark.
    check(hc._worker_holdings(PS_AGED) == {"task-old.txt": None},
          f"an over-deadline pid with no mark is UNKNOWN, not 54001s and not waiting "
          f"(got {hc._worker_holdings(PS_AGED)})")
    check(hasattr(hc, "WAITING_FOR_LOCK") and
          hc._worker_holdings(PS_FRESH) == {"task-new.txt": hc.WAITING_FOR_LOCK},
          "a 3s pid with no mark is still innocently WAITING — process age is not the runtime")
    check(hc._parse_etime("900:01") == 54001.0,
          "the ps ELAPSED parser itself is unchanged and still tested")
    # argv-only output (no ELAPSED column) must still yield the filename, age unknown.
    check(hc._worker_holdings(PS) == {"task-aaa.txt": None, "task-bbb.txt": None},
          f"argv-only ps -> names with unknown age (got {hc._worker_holdings(PS)})")

    # Every ownership case above hands _provider_runtime a pid directly; the
    # live `ps` never did, so the corpse check could not run in production.
    _pr = Path(tempfile.mkdtemp(prefix="prodfmt-"))
    (_pr / "tasks").mkdir()
    (_pr / "state" / "task-workstream-runs").mkdir(parents=True)
    (_pr / "state" / "task-workstream-runs" / "task-live.txt.started").write_text(
        json.dumps({"pid": 999999, "started": _time.time() - 4000}) + "\n")
    _ows = hc.WORKSPACE_DIR
    hc.WORKSPACE_DIR = _pr
    try:
        with hard_timeout("900"):
            # (a) the format production actually requests: a FOREIGN owner's mark
            #     must not age a worker that has run 3 seconds.
            prod = ("  4242    00:03 /usr/bin/python3 /r/skills/task-workstream-sessions/"
                    "scripts/session-worker.py --task-file /w/tasks/task-live.txt\n")
            got = hc._worker_holdings(prod)
            check(got == {"task-live.txt": hc.WAITING_FOR_LOCK},
                  f"a stale mark from pid 999999 is not inherited by pid 4242 (got {got})")

            # (b) No pid column -> owner unknowable. An unverifiable owner is
            #     not a matching one, so the corpse is still not trusted.
            noid = ("    00:03 /usr/bin/python3 /r/skills/task-workstream-sessions/"
                    "scripts/session-worker.py --task-file /w/tasks/task-live.txt\n")
            got = hc._worker_holdings(noid)
            check(got == {"task-live.txt": hc.WAITING_FOR_LOCK},
                  f"an UNIDENTIFIED owner does not inherit the mark either (got {got})")

            # (c) and the positive half, so (a)/(b) are not passing on a dead read.
            (_pr / "state" / "task-workstream-runs" / "task-live.txt.started").write_text(
                json.dumps({"pid": 4242, "started": _time.time() - 4000}) + "\n")
            got = hc._worker_holdings(prod)
            age = got.get("task-live.txt")
            check(isinstance(age, float) and 3900 < age < 4100,
                  f"its OWN mark still hands over the clock (got {age})")
    finally:
        hc.WORKSPACE_DIR = _ows

    # The production invocation is pinned by BEHAVIOUR, not by a source regex:
    # parse a line in the format the live call requests and require a pid back.
    _seen = {}
    _real = hc.subprocess.run

    def _capture(cmd, *a, **kw):
        if cmd[:2] == ["ps", "-Ao"]:
            _seen["fmt"] = cmd[2]
        return _real(cmd, *a, **kw)

    hc.subprocess.run = _capture
    try:
        hc._worker_holdings()
    finally:
        hc.subprocess.run = _real
    fmt = _seen.get("fmt", "")
    probe_line = _real(["ps", "-Ao", fmt], capture_output=True, text=True).stdout if fmt else ""
    first = next((l for l in probe_line.splitlines() if l.split()), "")
    check(bool(first) and first.split()[0].isdigit(),
          f"the live ps format {fmt!r} yields a pid the parser can read (got {first.split()[:1]})")

check(hc._tasks_held_by_a_worker(PS) == {"task-aaa.txt", "task-bbb.txt"},
      "the set form is unchanged for its existing callers")

# A runtime we cannot read must not buy silence — unknown is not progress.
if hasattr(hc, "_worker_holdings"):
    with hard_timeout("900"):
        d = Path(tempfile.mkdtemp()); (d / "tasks").mkdir()
        f = d / "tasks" / "task-000.txt"; f.write_text("x")
        os.utime(f, (_time.time() - 400, _time.time() - 400))
        ows, orig = hc.WORKSPACE_DIR, hc._worker_holdings
        hc.WORKSPACE_DIR = d
        hc._worker_holdings = lambda ps_output=None: {"task-000.txt": None}
        try:
            ru = hc.check_task_queue()
        finally:
            hc.WORKSPACE_DIR, hc._worker_holdings = ows, orig
        check(ru["status"] == "warn",
              f"unreadable worker runtime -> warn, never quiet (got {ru['status']!r})")
        check("could not be read" in ru["detail"],
              "and it says why progress cannot be established")

# ---- lock WAIT is not run time ------------------------------------------------
# Same-workstream tasks serialize on a lock taken AFTER the process starts.
MARK_PID = 4242


def _runtime_for(name, proc_age, pid=MARK_PID):
    """The module's own notion of runtime, or process age on a build that has
    none — so these cases FAIL at the merge-base instead of raising there."""
    if not hasattr(hc, "_provider_runtime"):
        return proc_age
    try:
        return hc._provider_runtime(name, proc_age, pid)
    except TypeError:      # a build whose reader has no pid parameter
        return hc._provider_runtime(name, proc_age)


def mark_probe(file_age, mark_age, deadline="900", proc_age=9999):
    """mark_age None = no run mark on disk, i.e. still waiting for the lock."""
    tmp = Path(tempfile.mkdtemp()); (tmp / "tasks").mkdir()
    f = tmp / "tasks" / "task-000.txt"; f.write_text("x")
    t0 = _time.time() - file_age; os.utime(f, (t0, t0))
    if mark_age is not None:
        md = tmp / "state" / "task-workstream-runs"; md.mkdir(parents=True)
        (md / "task-000.txt.started").write_text(json.dumps(
            {"pid": MARK_PID, "started": _time.time() - mark_age}) + "\n")
    ows, oh = hc.WORKSPACE_DIR, hc._worker_holdings
    hc.WORKSPACE_DIR = tmp
    # Only `ps` is stubbed; the REAL mark reader decides the runtime, with a
    # deliberately huge process age so a regression to pid-age cannot pass.
    hc._worker_holdings = lambda ps_output=None: {
        "task-000.txt": _runtime_for("task-000.txt", proc_age, MARK_PID)}
    try:
        with hard_timeout(deadline):
            return hc.check_task_queue()
    finally:
        hc.WORKSPACE_DIR, hc._worker_holdings = ows, oh

# THE PAIR, behavioural and unguarded. Innocent only INSIDE the deadline: past
# it, absent-mark and failed-write are indistinguishable.
r = mark_probe(5000, None, proc_age=300)
check(r["status"] == "ok",
      f"waiting for the lock, process under the deadline -> ok (got {r['status']!r})")
check("wedged" not in r["detail"], "and a waiter is never called wedged")

r = mark_probe(5000, 1200)
check(r["status"] == "warn",
      f"provider running 1200s past a 900s deadline -> warn (got {r['status']!r})")
# BOTH edges: rounding a start can render 1199, a slow runner 1201+. 9999 (the
# process age) is far outside either, so the guarantee survives the tolerance.
_m = re.search(r"running (\d+)s", r["detail"] or "")
check(bool(_m) and 1199 <= int(_m.group(1)) <= 1230,
      f"and it quotes the PROVIDER's runtime, not the 9999s process age "
      f"(got {_m.group(1) + 's' if _m else r['detail'][-60:]!r})")

r = mark_probe(5000, 5)
check(r["status"] == "ok",
      f"fresh provider run on an old pid -> ok (got {r['status']!r})")

check(hasattr(hc, "_provider_runtime"),
      "runtime comes from a named reader, not from process age")

if hasattr(hc, "_provider_runtime"):
    # The mark reader itself, away from check_task_queue.
    tmpm = Path(tempfile.mkdtemp()); ows = hc.WORKSPACE_DIR; hc.WORKSPACE_DIR = tmpm
    try:
        check(hc._provider_runtime("nope.txt", 500) == hc.WAITING_FOR_LOCK,
              "absent mark + live process = WAITING_FOR_LOCK, not a runtime")
        check(hc._provider_runtime("nope.txt", None) is None,
              "absent mark + no process = unknown, not waiting")
        md = tmpm / "state" / "task-workstream-runs"; md.mkdir(parents=True)
        (md / "bad.txt.started").write_text("not-a-float\n")
        check(hc._provider_runtime("bad.txt", 10) is None,
              "an unreadable mark is unknown, never zero")
    finally:
        hc.WORKSPACE_DIR = ows

    # A waiter must not be able to silence a genuinely wedged sibling.
    tmp2 = Path(tempfile.mkdtemp()); (tmp2 / "tasks").mkdir()
    for i, ma in ((0, None), (1, 1200)):
        f = tmp2 / "tasks" / f"task-{i:03d}.txt"; f.write_text("x")
        t0 = _time.time() - 5000; os.utime(f, (t0, t0))
        if ma is not None:
            md = tmp2 / "state" / "task-workstream-runs"; md.mkdir(parents=True, exist_ok=True)
            (md / f"task-{i:03d}.txt.started").write_text(f"{_time.time() - ma:.3f}\n")
    ows, oh = hc.WORKSPACE_DIR, hc._worker_holdings
    hc.WORKSPACE_DIR = tmp2
    hc._worker_holdings = lambda ps_output=None: {
        n: _runtime_for(n, 9999) for n in ("task-000.txt", "task-001.txt")}
    try:
        with hard_timeout("900"):
            rmix = hc.check_task_queue()
    finally:
        hc.WORKSPACE_DIR, hc._worker_holdings = ows, oh
    check(rmix["status"] == "warn",
          f"one waiter + one wedged sibling -> warn (got {rmix['status']!r})")

# ---- the mark identifies its OWNER, and absence must not buy silence ---------
# A `finally` does not survive SIGKILL; a failed write leaves no mark at all.
def owner_probe(proc_age, mark, deadline="900"):
    """mark: None, or (owner_pid, age_seconds)."""
    tmp = Path(tempfile.mkdtemp()); (tmp / "tasks").mkdir()
    f = tmp / "tasks" / "task-000.txt"; f.write_text("x")
    t0 = _time.time() - 5000; os.utime(f, (t0, t0))
    if mark is not None:
        owner, age = mark
        md = tmp / "state" / "task-workstream-runs"; md.mkdir(parents=True)
        (md / "task-000.txt.started").write_text(json.dumps(
            {"pid": owner, "started": _time.time() - age}) + "\n")
    ows, oh = hc.WORKSPACE_DIR, hc._worker_holdings
    hc.WORKSPACE_DIR = tmp
    hc._worker_holdings = lambda ps_output=None: {
        "task-000.txt": _runtime_for("task-000.txt", proc_age, MARK_PID)}
    try:
        with hard_timeout(deadline):
            return hc.check_task_queue()
    finally:
        hc.WORKSPACE_DIR, hc._worker_holdings = ows, oh

# THE PAIR: a failed write must never read as healthy...
r = owner_probe(1200, None)
check(r["status"] == "warn",
      f"no mark + a 1200s process past a 900s deadline -> warn (got {r['status']!r})")
# ...while a genuinely waiting worker inside the deadline still does.
r = owner_probe(300, None)
check(r["status"] == "ok",
      f"no mark + a 300s process under the deadline -> ok (got {r['status']!r})")

# A mark another pid left behind must not age a fresh run.
r = owner_probe(5, (9999, 1200))
check(r["status"] == "ok",
      f"stale mark from pid 9999 + a 5s worker -> ok (got {r['status']!r})")
r = owner_probe(1300, (MARK_PID, 1200))
check(r["status"] == "warn",
      f"own mark, provider 1200s -> warn (got {r['status']!r})")

_PID_AWARE = False
if hasattr(hc, "_provider_runtime"):
    import inspect as _inspect
    _PID_AWARE = len(_inspect.signature(hc._provider_runtime).parameters) >= 3
check(_PID_AWARE, "the runtime reader takes the OWNING pid, so a stale mark cannot be inherited")

if _PID_AWARE:
    d = Path(tempfile.mkdtemp()); md = d / "state" / "task-workstream-runs"
    md.mkdir(parents=True)
    ows = hc.WORKSPACE_DIR; hc.WORKSPACE_DIR = d
    try:
        (md / "own.txt.started").write_text(json.dumps({"pid": 7, "started": _time.time() - 30}))
        check(abs(hc._provider_runtime("own.txt", 100, 7) - 30) < 5,
              "a mark owned by the asking pid yields its runtime")
        check(hc._provider_runtime("own.txt", 100, 8) == hc.WAITING_FOR_LOCK,
              "the same mark asked by ANOTHER pid is not inherited")
        (md / "junk.txt.started").write_text("not json")
        check(hc._provider_runtime("junk.txt", 100, 7) is None,
              "an unparseable mark is unknown, never zero and never waiting")
        (md / "nopid.txt.started").write_text(json.dumps({"started": _time.time()}))
        check(hc._provider_runtime("nopid.txt", 100, 7) is None,
              "a mark without an owner is unusable, not trusted")
    finally:
        hc.WORKSPACE_DIR = ows

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all task-queue in-flight assertions passed")
