#!/usr/bin/env python3
"""
Tests for `check_task_watcher` — direct liveness of the streaming task watcher.

Motivated by 2026-07-21: the watcher was dead, tasks/ was empty, and
health-check reported 0 failures. Neither existing consequence check can see
that state — `check_task_queue` needs >3 tasks AND >300s age (a single
stranded owner DM never trips the count), and `check_core_proactive_loop`
reads core-status.json, which is freshest precisely when the loop is alive
and the watcher is not.

Covers:
  a) no core alive → ok (watcher not expected; must not latch red on hosts
     that simply aren't running Sutando)
  b) core alive, sentinel absent → warn
  c) core alive, sentinel holds a dead PID → warn (crashed, sentinel left behind)
  d) core alive, PID alive but argv is not the watcher → warn (PID reuse)
  e) core alive, PID alive and argv names the watcher → ok
  e2) sentinel names an observer that merely MENTIONS the script → never ok,
     before or after the cleanup it prescribes (anchored predicate, not substring)
  f) core alive, sentinel unparseable → warn (not a crash)
  g) the check is registered in run_checks' output
  h) _proc_argv against real PIDs (live + nonexistent) — the OS-facing half
  i) _proc_argv swallows a probe failure rather than failing the health check
  r) supervised watcher + absent sentinel exposes the pid --fix can re-stamp
  s) --fix re-stamps it and the RE-RUN check reports ok (no restart needed)
  t) --fix refuses to stamp a pid that is no longer the watcher (PID reuse)
  t2) ...including a process that merely MENTIONS the script (an observer, a
     `ps | grep`) — the fixer uses the file's exact predicate, not a substring
  t3) ...and the same holds for the second, post-publication argv test
  u) --fix never clobbers a sentinel a watcher re-claimed meanwhile
  u2) ...nor one claimed INSIDE the write window, where only the kernel can
     arbitrate (an exists()-then-write cannot see it)
  u3) a pid that stops being the watcher mid-write has its stamp withdrawn —
     the pre-write probe is a snapshot, so publication is re-validated
  u4) ...and a withdrawal the OS denied is reported as such, never as done
  v) a check with no repairable pid is declined, not stamped with junk
  w) an unwritable state dir is reported, never raised into the caller
  w2) ...including when it is the exclusive create, not the mkdir, that fails
  x) `--fix` actually REACHES the repair (warn never enters `issues`)
  y) under `--json` the repair line stays off stdout, so JSON still parses
  y2) ...and the repair pass's own `_`-prefixed keys stay out of the payload

Run: python3 tests/health-check-task-watcher.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import shutil
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_helpers"))
from os_probes import PS_SKIP_REASON, ps_available  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

MOD_PATH = REPO / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("health_check", MOD_PATH)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

# Captured before any case swaps it in, so a failing case cannot leak a stub.
_REAL_PROC_ARGV = hc._proc_argv


def make_workspace(td: Path, *, core_alive: bool, pid_text: str | None) -> Path:
    """Build a temp workspace. `core_alive` stamps a fresh heartbeat file;
    `pid_text=None` means no sentinel at all."""
    state = td / "state"
    state.mkdir(parents=True, exist_ok=True)
    if core_alive:
        cores = state / "cores"
        cores.mkdir(exist_ok=True)
        # Host-labelled: the probe asks whether THIS host's core is alive,
        # so a fixed name would only ever satisfy the fleet-wide reader.
        beat = cores / f"{hc._host_label()}.alive"
        beat.write_text("{}")
        # _any_core_alive uses a 90s window; a just-written file is inside it.
    if pid_text is not None:
        (state / "watch-tasks-stream.pid").write_text(pid_text)
    return td


def run_check(*, core_alive: bool, pid_text: str | None, argv: str | None = None,
              trees: dict | None = None, parents: dict | None = None) -> dict:
    """Call check_task_watcher against a temp WORKSPACE_DIR. `argv` patches
    the _proc_argv probe: None = leave the real one (only used where no PID
    is read), "" = process gone, any string = that process's argv.

    `parents` maps a fabricated tree root to its parent pid; a root absent from
    it has unknown parentage. The parent probes are stubbed unconditionally —
    `trees` invents pids, and an unstubbed probe reads the HOST's process table,
    where a fabricated pid may really exist and carry a real parent.
    """
    with tempfile.TemporaryDirectory() as td:
        make_workspace(Path(td), core_alive=core_alive, pid_text=pid_text)
        saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
                 hc._ps_snapshot, hc._pid_parent, hc._local_core_pids)
        try:
            hc.WORKSPACE_DIR = Path(td)
            if argv is not None:
                hc._proc_argv = lambda pid: argv
            hc._watcher_trees = lambda *a, **k: (trees or {})
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda pid, ps=None: (parents or {}).get(pid)
            # Unstubbed this reads the HOST's tmux, the leak this fixture already
            # avoids for parents; a fabricated parent stands in for the core.
            hc._local_core_pids = lambda: {v for v in (parents or {}).values() if v != "1"}
            return hc.check_task_watcher()
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
             hc._ps_snapshot, hc._pid_parent, hc._local_core_pids) = saved


@contextlib.contextmanager
def supervised_watcher(*, pid: str = "7100", pid_text: str | None = None,
                       argv: str = "bash src/watch-tasks-stream.sh"):
    """A workspace the caller can INSPECT after the check — `run_check` deletes
    its tempdir, so the repair cases (which assert on a written file) need this.

    Patches the probes into the ONE state `--fix` repairs: a single watcher
    tree whose parent is a live session (not init), i.e. supervised.
    """
    with tempfile.TemporaryDirectory() as td:
        ws = make_workspace(Path(td), core_alive=True, pid_text=pid_text)
        saved = (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
                 hc._ps_snapshot, hc._pid_parent, hc._local_core_pids)
        try:
            hc.WORKSPACE_DIR = ws
            hc._proc_argv = lambda p: argv
            hc._watcher_trees = lambda *a, **k: {pid: {pid}}
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda p, ps=None: "500"
            # 500 stands in for the core session that spawned this watcher;
            # unstubbed, this would read the HOST's tmux.
            hc._local_core_pids = lambda: {"500"}
            yield ws
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
             hc._ps_snapshot, hc._pid_parent, hc._local_core_pids) = saved


def case_r_supervised_watcher_exposes_restamp_pid() -> list[str]:
    with supervised_watcher() as ws:
        r = hc.check_task_watcher()
    fails = []
    if r["status"] != "warn":
        fails.append(f"r) expected warn, got {r['status']}")
    if r.get("_sentinel_restamp_pid") != "7100":
        fails.append("r) the repairable pid must be exposed as data, not left "
                     f"for --fix to parse out of prose: {r!r}")
    return fails


def case_s_fix_restamps_and_recheck_is_ok() -> list[str]:
    fails = []
    with supervised_watcher() as ws:
        checks = [hc.check_task_watcher()]
        buf = io.StringIO()
        hc.apply_task_watcher_sentinel_fix(checks, stream=buf)
        sentinel = ws / "state" / "watch-tasks-stream.pid"
        if not sentinel.exists() or sentinel.read_text().strip() != "7100":
            fails.append("s) --fix must write the live watcher's pid, got "
                         f"{sentinel.read_text().strip() if sentinel.exists() else '<ABSENT>'}")
        # The check dict must carry the POST-fix state, re-measured.
        if checks[0]["status"] != "ok":
            fails.append(f"s) re-run check should be ok, got {checks[0]}")
        if "_sentinel_restamp_pid" in checks[0]:
            fails.append("s) the repaired check still advertises a repair")
        if "re-stamped" not in buf.getvalue():
            fails.append(f"s) the repair should be reported, got {buf.getvalue()!r}")
    return fails


def case_t_fix_refuses_a_recycled_pid() -> list[str]:
    """Between the check and the repair the pid can become someone else's.
    Stamping it would author the PID-reuse lie the probe exists to catch."""
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        hc._proc_argv = lambda p: "/usr/sbin/cupsd -l"
        msg = hc.fix_task_watcher_sentinel(check)
        if (ws / "state" / "watch-tasks-stream.pid").exists():
            fails.append("t) --fix stamped a pid that is no longer the watcher")
        if "no longer the watcher" not in msg:
            fails.append(f"t) should say why it refused, got {msg!r}")
    return fails


def case_t2_an_impostor_that_merely_mentions_the_script_is_refused() -> list[str]:
    """Case (t) uses an argv with no mention of the script, which a substring
    test also rejects — so it cannot tell the two predicates apart. This argv
    CONTAINS `watch-tasks-stream` without being the watcher: an observer, or
    the `ps`/grep wrapper the module's own comment at `_is_watcher_argv` names.

    `_is_watcher_argv` is the file's exact predicate and is what `_watcher_trees`
    already uses; the fixer must not carry a looser private copy, because the
    pid it stamps is the one the Stop hook later kills."""
    fails = []
    for argv in ("python3 observer.py watch-tasks-stream.sh",
                 "bash -c ps aux | grep watch-tasks-stream"):
        with supervised_watcher() as ws:
            check = hc.check_task_watcher()
            hc._proc_argv = lambda p, a=argv: a
            msg = hc.fix_task_watcher_sentinel(check)
            pid_file = ws / "state" / "watch-tasks-stream.pid"
            if pid_file.exists():
                fails.append(f"t2) --fix stamped an impostor ({argv!r}): "
                             f"sentinel now {pid_file.read_text().strip()}")
            if "no longer the watcher" not in msg:
                fails.append(f"t2) should refuse {argv!r}, got {msg!r}")
    return fails


def case_t3_an_impostor_appearing_mid_write_is_withdrawn() -> list[str]:
    """The post-publication re-validation is a SECOND argv test, and (t2) cannot
    reach it — a pre-write refusal returns before the write. Drive the impostor
    in on the second probe call so only the post-write check sees it."""
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        calls = {"n": 0}

        def _watcher_then_impostor(p):
            calls["n"] += 1
            return ("bash src/watch-tasks-stream.sh" if calls["n"] == 1
                    else "python3 observer.py watch-tasks-stream.sh")

        hc._proc_argv = _watcher_then_impostor
        msg = hc.fix_task_watcher_sentinel(check)
        pid_file = ws / "state" / "watch-tasks-stream.pid"
        if calls["n"] < 2:
            fails.append(f"t3) the post-write probe never ran ({calls['n']} call(s)) — "
                         "this case covers nothing")
        if pid_file.exists():
            fails.append("t3) an impostor seen after publication was left stamped: "
                         f"{pid_file.read_text().strip()}")
        if "mid-write" not in msg:
            fails.append(f"t3) should report the withdrawal, got {msg!r}")
    return fails


def case_u_fix_never_clobbers_a_reclaimed_sentinel() -> list[str]:
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        # A watcher claimed the sentinel after the check ran.
        (ws / "state" / "watch-tasks-stream.pid").write_text("9999\n")
        msg = hc.fix_task_watcher_sentinel(check)
        held = (ws / "state" / "watch-tasks-stream.pid").read_text().strip()
        if held != "9999":
            fails.append(f"u) --fix overwrote a live claim: sentinel now {held}")
        if "re-claimed" not in msg:
            fails.append(f"u) should say why it declined, got {msg!r}")
    return fails


def case_u2_competing_claim_inside_the_write_window_survives() -> list[str]:
    """Case (u) covers the SEQUENTIAL order — the sentinel already exists when
    the fixer runs, so any pre-write existence check sees it. This one lands
    the claim INSIDE the window instead: after that check, before publication.
    The fixer's `mkdir` of the state dir is its only call in that window, so
    claiming from mkdir is how a unit fixture schedules a write there.

    An exists()-then-write_text() truncates the newer claim; an exclusive
    create cannot, because the kernel — not the fixer — arbitrates."""
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        pid_file = ws / "state" / "watch-tasks-stream.pid"
        real_mkdir = Path.mkdir

        def _mkdir_then_claim(self, *a, **kw):
            result = real_mkdir(self, *a, **kw)
            if Path(self) == pid_file.parent and not pid_file.exists():
                pid_file.write_text("9999\n")   # a starting watcher's own stamp
            return result

        Path.mkdir = _mkdir_then_claim
        try:
            msg = hc.fix_task_watcher_sentinel(check)
        finally:
            Path.mkdir = real_mkdir
        held = pid_file.read_text().strip() if pid_file.exists() else "<ABSENT>"
        if held != "9999":
            fails.append(f"u2) --fix truncated a claim made mid-write: sentinel now {held}")
        if "re-claimed" not in msg:
            fails.append(f"u2) should report the lost race, got {msg!r}")
    return fails


def case_u3_pid_stale_after_publication_is_withdrawn() -> list[str]:
    """The pre-write argv probe is a snapshot; the pid can stop being the
    watcher before the file lands. Leaving that stamp would author the exact
    PID-reuse lie the probe exists to catch, and the Stop hook kills what this
    file names."""
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        pid_file = ws / "state" / "watch-tasks-stream.pid"
        seen = {"n": 0}

        def _argv_then_exit(pid):
            seen["n"] += 1
            return "bash src/watch-tasks-stream.sh" if seen["n"] == 1 else "zsh -l"

        hc._proc_argv = _argv_then_exit
        try:
            msg = hc.fix_task_watcher_sentinel(check)
        finally:
            hc._proc_argv = _REAL_PROC_ARGV
        if pid_file.exists():
            fails.append(f"u3) left a stamp for a dead watcher: {pid_file.read_text()!r}")
        if "withdrawn" not in msg:
            fails.append(f"u3) should report the withdrawal, got {msg!r}")
    return fails


def case_u4_a_withdrawal_that_failed_is_not_reported_as_done() -> list[str]:
    """(u3) with the retraction's own unlink denied. The fixer must not raise,
    and must not claim a withdrawal it could not perform — a stamp naming a
    dead pid is exactly what the operator has to know is still there."""
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        state = ws / "state"
        pid_file = state / "watch-tasks-stream.pid"
        seen = {"n": 0}

        def _argv_then_lock(pid):
            seen["n"] += 1
            if seen["n"] == 1:
                return "bash src/watch-tasks-stream.sh"
            state.chmod(0o555)      # file still readable, dir denies unlink
            return "zsh -l"

        hc._proc_argv = _argv_then_lock
        try:
            msg = hc.fix_task_watcher_sentinel(check)
        except OSError as e:
            state.chmod(0o755)
            return [f"u4) raised instead of reporting: {e!r}"]
        finally:
            hc._proc_argv = _REAL_PROC_ARGV
        state.chmod(0o755)
        if not pid_file.exists():
            fails.append("u4) the unlink was supposed to be denied — this case "
                         "covers nothing if the stamp actually went away")
        if "could not be withdrawn" not in msg:
            fails.append(f"u4) must report the failed withdrawal, got {msg!r}")
    return fails


def case_w2_unwritable_state_dir_is_reported() -> list[str]:
    """(w) fails in `mkdir`; this one gets past it — the dir exists, so
    `exist_ok=True` succeeds — and fails in the exclusive create instead.
    Both are write failures the fixer owes the operator as text."""
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        state = ws / "state"
        state.chmod(0o555)
        try:
            msg = hc.fix_task_watcher_sentinel(check)
        except OSError as e:
            state.chmod(0o755)
            return [f"w2) raised instead of reporting: {e!r}"]
        state.chmod(0o755)
        if (state / "watch-tasks-stream.pid").exists():
            fails.append("w2) the create was supposed to be denied — this case "
                         "covers nothing if the sentinel was written")
        if "could not write" not in msg:
            fails.append(f"w2) should report the write failure, got {msg!r}")
    return fails


def case_v_fix_declines_without_a_pid() -> list[str]:
    fails = []
    with supervised_watcher() as ws:
        msg = hc.fix_task_watcher_sentinel({"name": "task-watcher", "status": "warn"})
        if (ws / "state" / "watch-tasks-stream.pid").exists():
            fails.append("v) --fix wrote a sentinel with no pid to write")
        if "no re-stampable" not in msg:
            fails.append(f"v) should decline explicitly, got {msg!r}")
    return fails


def case_w_fix_reports_a_write_failure() -> list[str]:
    """--fix runs inside health-check; an OSError here would abort every later
    repair pass, so the fixer must return the failure as text."""
    fails = []
    with supervised_watcher() as ws:
        check = hc.check_task_watcher()
        shutil.rmtree(ws / "state")
        (ws / "state").write_text("not a directory\n")
        try:
            msg = hc.fix_task_watcher_sentinel(check)
        except OSError as e:
            return [f"w) raised instead of reporting: {e!r}"]
        if "could not write" not in msg:
            fails.append(f"w) should report the write failure, got {msg!r}")
    return fails


def _run_main_fix(argv: list[str]) -> tuple[bool, str, str]:
    """Run main() over a fresh module with the repair stubbed.

    A unit test of the fixer proves the decision, never that anything calls it:
    `warn` is excluded from `issues` by construction, so the wiring is the part
    that historically broke (see health-check-symlink-fix-reachable).
    """
    spec = importlib.util.spec_from_file_location("hc_wiring", MOD_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    called = []
    m.fix_task_watcher_sentinel = lambda c: (called.append(c["name"]), "re-stamped")[1]
    m.check_task_watcher = lambda: {"name": "task-watcher", "status": "ok",
                                    "detail": "re-measured: streaming watcher alive"}
    m.run_all_checks = lambda: [{"name": "task-watcher", "status": "warn",
                                 "_sentinel_restamp_pid": "7100", "detail": "no sentinel"}]
    saved, sys.argv = sys.argv, ["health-check.py"] + argv
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.main()
    except SystemExit:
        pass
    finally:
        sys.argv = saved
    return bool(called), out.getvalue(), err.getvalue()


def case_x_fix_is_reachable_from_main() -> list[str]:
    fired, out, _ = _run_main_fix(["--fix"])
    fails = []
    if not fired:
        fails.append("x) --fix never reached the task-watcher repair")
    if "re-measured" not in out:
        fails.append(f"x) the reported verdict must come from the RE-RUN, got {out!r}")
    return fails


def case_y_json_repair_line_goes_to_stderr() -> list[str]:
    fired, out, err = _run_main_fix(["--fix", "--json"])
    fails = []
    if not fired:
        fails.append("y) --fix --json never reached the repair")
    try:
        json.loads(out)
    except ValueError:
        fails.append(f"y) the repair line corrupted the JSON on stdout: {out!r}")
    if "re-stamped" not in err:
        fails.append(f"y) the repair should still be reported on stderr, got {err!r}")
    return fails


def case_y2_private_keys_stay_out_of_the_json_payload() -> list[str]:
    """`_sentinel_restamp_pid` is the fix pass's internal channel, and whether it
    is present depends on the flags — so `--json` must not publish it."""
    fails = []
    _fired, out, _err = _run_main_fix(["--json"])
    try:
        payload = json.loads(out)
    except ValueError:
        return [f"y2) --json did not emit parseable JSON: {out!r}"]
    checks = payload.get("checks") or []
    if not checks:
        return ["y2) the payload carried no checks — this case covers nothing"]
    leaked = sorted({k for c in checks for k in c if k.startswith("_")})
    if leaked:
        fails.append(f"y2) private key(s) reached --json: {leaked}")
    if payload.get("total") != len(checks):
        fails.append(f"y2) filtering changed the count: total={payload.get('total')} "
                     f"vs {len(checks)} check(s)")
    if checks[0].get("name") != "task-watcher" or "detail" not in checks[0]:
        fails.append(f"y2) the public fields must survive the filter, got {checks[0]!r}")
    return fails


def case_a_no_core_is_ok() -> list[str]:
    # The anti-latch guard: a host with no core running must not sit red.
    r = run_check(core_alive=False, pid_text=None)
    if r["status"] != "ok":
        return [f"a) no core alive should be ok, got {r['status']} ({r['detail']})"]
    return []


def case_b_sentinel_absent_warns() -> list[str]:
    r = run_check(core_alive=True, pid_text=None)
    if r["status"] != "warn":
        return [f"b) core alive + no sentinel should warn, got {r['status']}"]
    return []


def case_c_dead_pid_warns() -> list[str]:
    r = run_check(core_alive=True, pid_text="424242", argv="")
    if r["status"] != "warn":
        return [f"c) dead watcher PID should warn, got {r['status']}"]
    if "dead" not in r["detail"]:
        return [f"c) detail should name the crash, got {r['detail']!r}"]
    return []


def case_d_pid_reuse_warns() -> list[str]:
    # kill -0 alone would call this alive — the argv check is what catches it.
    r = run_check(core_alive=True, pid_text="4242", argv="/usr/sbin/cupsd -l")
    if r["status"] != "warn":
        return [f"d) PID reuse should warn, got {r['status']}"]
    if "reuse" not in r["detail"]:
        return [f"d) detail should name PID reuse, got {r['detail']!r}"]
    return []


def case_e_live_watcher_is_ok() -> list[str]:
    r = run_check(core_alive=True, pid_text="4242", argv="bash src/watch-tasks-stream.sh")
    if r["status"] != "ok":
        return [f"e) live watcher should be ok, got {r['status']} ({r['detail']})"]
    return []


def case_e2_an_observer_sentinel_is_never_blessed_across_cleanup() -> list[str]:
    """Cleanup driven twice with the sentinel naming a process that merely MENTIONS the
    script: pass 1 still names the live root; pass 2 (root gone, sentinel kept) is never ok."""
    fails = []
    for argv in ("python3 observer.py watch-tasks-stream.sh",
                 "bash -c ps aux | grep watch-tasks-stream"):
        before = run_check(core_alive=True, pid_text="4242", argv=argv,
                           trees={"100": {"100"}})
        after = run_check(core_alive=True, pid_text="4242", argv=argv, trees={})
        if "ownerless (1): 100" not in before["detail"] or "start exactly ONE" not in before["detail"]:
            fails.append(f"e2) pass 1 must name root 100 for cleanup ({argv!r}): {before['detail']!r}")
        for label, r in (("pass 1", before), ("pass 2", after)):
            if r["status"] == "ok" or "alive (pid 4242)" in r["detail"]:
                fails.append(f"e2) {label} blessed the observer ({argv!r}): {r!r}")
    return fails


def case_f_unparseable_sentinel_warns() -> list[str]:
    r = run_check(core_alive=True, pid_text="not-a-pid", argv="")
    if r["status"] != "warn":
        return [f"f) unparseable sentinel should warn, got {r['status']}"]
    if "dead" in r["detail"]:
        return ["f) an unreadable sentinel is not a crash — detail should not say 'dead'"]
    return []


def case_g_registered_in_run_checks() -> list[str]:
    """A check nobody calls is not a check. Guards the registration line.

    Match the full `checks.append(...)` call, NOT the bare `check_task_watcher()`:
    that shorter string is a substring of the function's own `def` line, so it
    matches whether or not the check is ever registered — the first version of
    this case was vacuous for exactly that reason (caught by deleting the
    registration and watching the suite stay green).
    """
    src = (REPO / "src" / "health-check.py").read_text()
    if "checks.append(check_task_watcher())" not in src:
        return ["g) check_task_watcher() is never appended to the checks list"]
    return []


def case_h_proc_argv_reads_a_real_process() -> list[str]:
    """Exercise the real probe, not the stub the cases above patch in.

    This is the half that talks to the OS, so it needs to run against actual
    PIDs or nothing verifies that `ps -p <pid> -o args=` returns what the
    caller expects.
    """
    fails = []
    if not ps_available():
        # Loud, never silent: without ps this case would assert '' == '' and
        # pass for the wrong reason, which is worse than not running it.
        print(f"      SKIP h) {PS_SKIP_REASON}")
        return fails
    mine = hc._proc_argv(os.getpid())
    if not mine:
        fails.append("h) _proc_argv(os.getpid()) returned empty for a live process")
    elif "python" not in mine.lower():
        fails.append(f"h) argv for this process should name the interpreter, got {mine[:60]!r}")
    # A PID that cannot be running: above the platform maximum.
    gone = hc._proc_argv(4_000_000)
    if gone != "":
        fails.append(f"h) a nonexistent PID should give '', got {gone[:40]!r}")
    return fails


def case_i_proc_argv_swallows_probe_failure() -> list[str]:
    """A broken/absent `ps` must not take the health check down with it —
    the probe degrades to 'no argv', which the caller reads as 'not running'."""
    orig = hc.subprocess.run
    try:
        hc.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("ps missing"))
        got = hc._proc_argv(1)
    finally:
        hc.subprocess.run = orig
    if got != "":
        return [f"i) a raising probe should return '', got {got!r}"]
    return []



def case_j_extra_tree_warns() -> list[str]:
    """A live sentinel does not mean a healthy watcher layer: an orphan from an
    earlier start keeps draining tasks/ too, so every task is processed twice.
    Observed 2026-07-21 — two monitors reported the same TASK_FILE."""
    r = run_check(core_alive=True, pid_text="4242",
                  argv="bash src/watch-tasks-stream.sh",
                  trees={"4200": {"4200", "4242"}, "9000": {"9000", "9001"}})
    fails = []
    if r["status"] != "warn":
        fails.append(f"j) an untracked extra tree should warn, got {r['status']}")
    if "9000" not in r["detail"]:
        fails.append(f"j) detail must name the untracked root, got {r['detail']!r}")
    # "not an extra" is the extras COUNT and the stoppable group, not a bare
    # substring: the sentinel's root is now named in a protected group.
    if "1 not tracked by the sentinel" not in r["detail"]:
        fails.append(f"j) sentinel's tree inflated the extras count: {r['detail']!r}")
    stoppable = re.search(r"ownerless \((\d+)\): ([^;]*)", r["detail"])
    if stoppable and "4200" in stoppable.group(2):
        fails.append("j) must NOT offer the sentinel's own tree for stopping")
    return fails


def case_k_sentinels_own_tree_is_not_an_extra() -> list[str]:
    """The sentinel records the SCRIPT's pid, not its shell wrapper's, so the
    tree containing it must be recognised as the tracked one — otherwise the
    check tells the operator to kill the watcher it just told them to keep."""
    r = run_check(core_alive=True, pid_text="4242",
                  argv="bash src/watch-tasks-stream.sh",
                  trees={"4200": {"4200", "4242", "4243"}})
    if r["status"] != "ok":
        return [f"k) sole tree owning the sentinel should be ok, got {r['status']} ({r['detail']})"]
    return []


def case_l_dead_sentinel_with_live_orphan() -> list[str]:
    """Saying 'not running' here would be false — tasks ARE being drained, and
    restarting on that basis is what creates the duplicates."""
    r = run_check(core_alive=True, pid_text="424242", argv="",
                  trees={"9000": {"9000", "9001"}})
    fails = []
    if r["status"] != "warn":
        fails.append(f"l) expected warn, got {r['status']}")
    if "ownerless (1): 9000" not in r["detail"]:
        fails.append(f"l) detail should name the orphan, got {r['detail']!r}")
    if "IS being drained" not in r["detail"]:
        fails.append("l) must not claim tasks/ is unattended when a watcher runs")
    return fails


def case_m_absent_sentinel_with_live_orphan() -> list[str]:
    r = run_check(core_alive=True, pid_text=None, trees={"9000": {"9000"}})
    fails = []
    if r["status"] != "warn":
        fails.append(f"m) expected warn, got {r['status']}")
    if "ownerless (1): 9000" not in r["detail"]:
        fails.append(f"m) detail should name the orphan, got {r['detail']!r}")
    return fails


def case_m2_fabricated_pid_ignores_the_host_process_table() -> list[str]:
    """`trees` invents pids; the verdict must not depend on whether the host
    happens to be running one. Unstubbed, the parent probe read the real table,
    so on a runner where 9000 existed under kthreadd case m flipped to the
    supervised branch — a green suite elsewhere and a red one there."""
    saved = hc._pid_parent
    try:
        hc._pid_parent = lambda pid, ps=None: "2"   # host really runs pid 9000
        r = run_check(core_alive=True, pid_text=None, trees={"9000": {"9000"}})
    finally:
        hc._pid_parent = saved
    if "ownerless (1): 9000" not in r["detail"]:
        return [f"m2) host pid table leaked into the verdict, got {r['detail']!r}"]
    return []


def case_n_trees_group_a_process_chain() -> list[str]:
    """The grouping algorithm: script + subshell is ONE watcher, not two.
    Counting matching processes would double it."""
    ps = (
        "  100     1 /bin/zsh -c eval 'bash src/watch-tasks-stream.sh'\n"
        "  101   100 bash src/watch-tasks-stream.sh\n"
        "  102   101 bash src/watch-tasks-stream.sh\n"
        "  200     1 /bin/zsh -c eval 'bash src/watch-tasks-stream.sh'\n"
        "  201   200 bash src/watch-tasks-stream.sh\n"
        "  999     1 python3 src/health-check.py\n"
    )
    trees = hc._watcher_trees(ps)
    fails = []
    if len(trees) != 2:
        fails.append(f"n) expected 2 trees, got {len(trees)}: {trees}")
    if "101" in trees and "102" not in trees["101"]:
        fails.append("n) the subshell must be grouped under its script root")
    if any("999" in members for members in trees.values()):
        fails.append("n) a non-watcher process leaked into a tree")
    return fails


def case_n2_mentioning_the_script_is_not_running_it() -> list[str]:
    """The observer trap: a substring test counts any shell whose command line
    contains the script name — including the one running the query. Observed
    2026-07-21: a loose match reported 3 trees where 2 were real."""
    ps = (
        "  101     1 bash src/watch-tasks-stream.sh\n"
        "  300     1 grep watch-tasks-stream\n"
        "  301     1 /bin/zsh -c ps -Ao pid,args | grep watch-tasks-stream.sh\n"
        "  302     1 /bin/zsh -c source /tmp/snap.sh && eval 'bash src/watch-tasks-stream.sh'\n"
    )
    trees = hc._watcher_trees(ps)
    fails = []
    if len(trees) != 1:
        fails.append(f"n2) only pid 101 is running the script; got {len(trees)} trees: {trees}")
    if "101" not in trees:
        fails.append(f"n2) the real watcher must be found, got {sorted(trees)}")
    return fails


def case_o_trees_excludes_our_own_pid() -> list[str]:
    """Guards the self-match trap: a caller whose argv happens to contain the
    search string must not count itself as a watcher."""
    ps = f"  {os.getpid()}     1 bash src/watch-tasks-stream.sh\n"
    trees = hc._watcher_trees(ps)
    if trees:
        return [f"o) our own pid must be excluded, got {trees}"]
    return []



def case_p_trees_runs_real_ps() -> list[str]:
    """Exercise the OS-facing half — the cases above all inject ps output, so
    without this the actual subprocess call ships untested (the same gap the
    coverage gate caught for _proc_argv)."""
    trees = hc._watcher_trees()
    if not isinstance(trees, dict):
        return [f"p) expected a dict from the real probe, got {type(trees).__name__}"]
    for root, members in trees.items():
        if not isinstance(members, set) or root not in members and not members:
            return [f"p) malformed tree entry {root!r}: {members!r}"]
    return []


def case_q_trees_swallows_probe_failure() -> list[str]:
    """A broken/absent ps must degrade to 'no watchers seen', not raise into
    the health check."""
    orig = hc.subprocess.run
    try:
        hc.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("ps missing"))
        got = hc._watcher_trees()
    finally:
        hc.subprocess.run = orig
    if got != {}:
        return [f"q) a raising probe should return {{}}, got {got!r}"]
    return []


def main() -> int:
    cases = [
        ("a", case_a_no_core_is_ok),
        ("b", case_b_sentinel_absent_warns),
        ("c", case_c_dead_pid_warns),
        ("d", case_d_pid_reuse_warns),
        ("e", case_e_live_watcher_is_ok),
        ("e2", case_e2_an_observer_sentinel_is_never_blessed_across_cleanup),
        ("f", case_f_unparseable_sentinel_warns),
        ("g", case_g_registered_in_run_checks),
        ("h", case_h_proc_argv_reads_a_real_process),
        ("i", case_i_proc_argv_swallows_probe_failure),
        ("j", case_j_extra_tree_warns),
        ("k", case_k_sentinels_own_tree_is_not_an_extra),
        ("l", case_l_dead_sentinel_with_live_orphan),
        ("m", case_m_absent_sentinel_with_live_orphan),
        ("m2", case_m2_fabricated_pid_ignores_the_host_process_table),
        ("n", case_n_trees_group_a_process_chain),
        ("n2", case_n2_mentioning_the_script_is_not_running_it),
        ("o", case_o_trees_excludes_our_own_pid),
        ("p", case_p_trees_runs_real_ps),
        ("q", case_q_trees_swallows_probe_failure),
        ("r", case_r_supervised_watcher_exposes_restamp_pid),
        ("s", case_s_fix_restamps_and_recheck_is_ok),
        ("t", case_t_fix_refuses_a_recycled_pid),
        ("t2", case_t2_an_impostor_that_merely_mentions_the_script_is_refused),
        ("t3", case_t3_an_impostor_appearing_mid_write_is_withdrawn),
        ("u", case_u_fix_never_clobbers_a_reclaimed_sentinel),
        ("u2", case_u2_competing_claim_inside_the_write_window_survives),
        ("u3", case_u3_pid_stale_after_publication_is_withdrawn),
        ("u4", case_u4_a_withdrawal_that_failed_is_not_reported_as_done),
        ("v", case_v_fix_declines_without_a_pid),
        ("w", case_w_fix_reports_a_write_failure),
        ("w2", case_w2_unwritable_state_dir_is_reported),
        ("x", case_x_fix_is_reachable_from_main),
        ("y", case_y_json_repair_line_goes_to_stderr),
        ("y2", case_y2_private_keys_stay_out_of_the_json_payload),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nTask-watcher liveness invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
