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
  f) core alive, sentinel unparseable → warn (not a crash)
  g) the check is registered in run_checks' output
  h) _proc_argv against real PIDs (live + nonexistent) — the OS-facing half
  i) _proc_argv swallows a probe failure rather than failing the health check
  r) supervised watcher + absent sentinel exposes the pid --fix can re-stamp
  s) --fix re-stamps it and the RE-RUN check reports ok (no restart needed)
  t) --fix refuses to stamp a pid that is no longer the watcher (PID reuse)
  u) --fix never clobbers a sentinel a watcher re-claimed meanwhile
  v) a check with no repairable pid is declined, not stamped with junk
  w) an unwritable state dir is reported, never raised into the caller
  x) `--fix` actually REACHES the repair (warn never enters `issues`)
  y) under `--json` the repair line stays off stdout, so JSON still parses

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
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MOD_PATH = REPO / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("health_check", MOD_PATH)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def make_workspace(td: Path, *, core_alive: bool, pid_text: str | None) -> Path:
    """Build a temp workspace. `core_alive` stamps a fresh heartbeat file;
    `pid_text=None` means no sentinel at all."""
    state = td / "state"
    state.mkdir(parents=True, exist_ok=True)
    if core_alive:
        cores = state / "cores"
        cores.mkdir(exist_ok=True)
        beat = cores / "testhost.alive"
        beat.write_text("{}")
        # _any_core_alive uses a 90s window; a just-written file is inside it.
    if pid_text is not None:
        (state / "watch-tasks-stream.pid").write_text(pid_text)
    return td


def run_check(*, core_alive: bool, pid_text: str | None, argv: str | None = None,
              trees: dict | None = None) -> dict:
    """Call check_task_watcher against a temp WORKSPACE_DIR. `argv` patches
    the _proc_argv probe: None = leave the real one (only used where no PID
    is read), "" = process gone, any string = that process's argv."""
    with tempfile.TemporaryDirectory() as td:
        make_workspace(Path(td), core_alive=core_alive, pid_text=pid_text)
        orig_ws, orig_probe, orig_trees = hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees
        try:
            hc.WORKSPACE_DIR = Path(td)
            if argv is not None:
                hc._proc_argv = lambda pid: argv
            hc._watcher_trees = lambda *a, **k: (trees or {})
            return hc.check_task_watcher()
        finally:
            hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees = orig_ws, orig_probe, orig_trees


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
                 hc._ps_snapshot, hc._pid_parent)
        try:
            hc.WORKSPACE_DIR = ws
            hc._proc_argv = lambda p: argv
            hc._watcher_trees = lambda *a, **k: {pid: {pid}}
            hc._ps_snapshot = lambda *a, **k: ""
            hc._pid_parent = lambda p, ps=None: "500"
            yield ws
        finally:
            (hc.WORKSPACE_DIR, hc._proc_argv, hc._watcher_trees,
             hc._ps_snapshot, hc._pid_parent) = saved


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
    if "4200" in r["detail"]:
        fails.append("j) must NOT list the sentinel's own tree as an extra")
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
    if "orphaned" not in r["detail"]:
        fails.append(f"l) detail should name the orphan, got {r['detail']!r}")
    if "IS being drained" not in r["detail"]:
        fails.append("l) must not claim tasks/ is unattended when a watcher runs")
    return fails


def case_m_absent_sentinel_with_live_orphan() -> list[str]:
    r = run_check(core_alive=True, pid_text=None, trees={"9000": {"9000"}})
    fails = []
    if r["status"] != "warn":
        fails.append(f"m) expected warn, got {r['status']}")
    if "orphaned" not in r["detail"]:
        fails.append(f"m) detail should name the orphan, got {r['detail']!r}")
    return fails


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
        ("f", case_f_unparseable_sentinel_warns),
        ("g", case_g_registered_in_run_checks),
        ("h", case_h_proc_argv_reads_a_real_process),
        ("i", case_i_proc_argv_swallows_probe_failure),
        ("j", case_j_extra_tree_warns),
        ("k", case_k_sentinels_own_tree_is_not_an_extra),
        ("l", case_l_dead_sentinel_with_live_orphan),
        ("m", case_m_absent_sentinel_with_live_orphan),
        ("n", case_n_trees_group_a_process_chain),
        ("n2", case_n2_mentioning_the_script_is_not_running_it),
        ("o", case_o_trees_excludes_our_own_pid),
        ("p", case_p_trees_runs_real_ps),
        ("q", case_q_trees_swallows_probe_failure),
        ("r", case_r_supervised_watcher_exposes_restamp_pid),
        ("s", case_s_fix_restamps_and_recheck_is_ok),
        ("t", case_t_fix_refuses_a_recycled_pid),
        ("u", case_u_fix_never_clobbers_a_reclaimed_sentinel),
        ("v", case_v_fix_declines_without_a_pid),
        ("w", case_w_fix_reports_a_write_failure),
        ("x", case_x_fix_is_reachable_from_main),
        ("y", case_y_json_repair_line_goes_to_stderr),
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
