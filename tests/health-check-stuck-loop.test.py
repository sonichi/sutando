#!/usr/bin/env python3
"""
Tests for the stuck-loop / queue-pileup detection added to health-check.py.

Motivated by the 2026-05-06 incident: 5 voice-queued tasks sat in tasks/ for
20+ minutes with no processing while core-status.json showed status="running"
with a 5-day-old timestamp. The detection layer that would have caught this
didn't exist; it does now.

Covers:
  Core proactive loop staleness — `check_core_proactive_loop`
    a) missing core-status.json → ok
    b) malformed JSON → ok (don't false-alarm on half-written files)
    c) status != "running" → ok regardless of age
    d) status="running" with fresh ts → ok
    e) status="running" with stale ts → warn
    f) status="running" with no ts → ok (defensive)

  Task-queue pileup — `check_task_queue`
    g) no tasks dir → ok
    h) empty queue → ok
    i) small queue, all fresh → ok
    j) large queue, all fresh → ok (count alone shouldn't alarm)
    k) small queue, all old → ok (age alone shouldn't alarm)
    l) large queue with old oldest → warn
    m) archive subdir is excluded from the count
    m2) completed tasks with canonical results are excluded from the count
    m3) completed tasks are excluded from core-recovery wedge detection
    m4) task scan errors fail open as an empty pending queue

  Notification dedup — `notify_for_failures`
    n) empty failures → no notification
    o) same set within 1h → only one notification
    p) different sets → both fire
    q) all-ok input → no state file touched

Run: python3 tests/health-check-stuck-loop.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

# Load src/health-check.py as `health_check` (filename has a hyphen, can't
# import directly).
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def make_checks(*statuses_and_names):
    """[(status, name), ...] → list of check dicts. Mirrors the pattern in
    health-check-emit-task.test.py."""
    return [{"name": n, "status": s, "detail": "test"} for (s, n) in statuses_and_names]


def write_status(workspace_dir: Path, payload: dict | None) -> None:
    """Write core-status.json into a temp WORKSPACE_DIR override; pass None
    to skip writing the file at all. (Was REPO_DIR pre-PR #836; status files
    moved under state/ — check_core_proactive_loop reads via status_read_path.)"""
    if payload is not None:
        state_dir = workspace_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "core-status.json").write_text(json.dumps(payload))


def write_task(tasks_dir: Path, name: str, age_sec: int) -> Path:
    p = tasks_dir / name
    p.write_text("test task body")
    mtime = time.time() - age_sec
    os.utime(p, (mtime, mtime))
    return p


# ---------------------------------------------------------------------------
# core-status.json staleness — check_core_proactive_loop
# ---------------------------------------------------------------------------

def with_repo_override(payload):
    """Run check_core_proactive_loop against a temp WORKSPACE_DIR. Returns
    the check dict. `payload=None` means don't write the status file.
    Renamed semantics: pre-PR #836 this mocked REPO_DIR; core-status.json
    now lives in WORKSPACE_DIR (per-user runtime state)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        write_status(td, payload)
        orig_ws = hc.WORKSPACE_DIR
        try:
            hc.WORKSPACE_DIR = td
            return hc.check_core_proactive_loop(threshold_sec=600)
        finally:
            hc.WORKSPACE_DIR = orig_ws


def case_a_status_missing() -> list[str]:
    fails = []
    r = with_repo_override(None)
    if r["status"] != "ok":
        fails.append(f"a) missing core-status.json should be ok, got {r['status']}")
    return fails


def case_b_status_malformed() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "core-status.json").write_text("{ this is not json")
        orig = hc.WORKSPACE_DIR
        try:
            hc.WORKSPACE_DIR = td
            r = hc.check_core_proactive_loop(threshold_sec=600)
        finally:
            hc.WORKSPACE_DIR = orig
    if r["status"] != "ok":
        fails.append(f"b) malformed JSON should be ok (don't false-alarm), got {r['status']}")
    return fails


def case_c_status_not_running() -> list[str]:
    fails = []
    # Idle status — even with a very old ts, should be ok.
    r = with_repo_override({"status": "idle", "ts": int(time.time()) - 86400})
    if r["status"] != "ok":
        fails.append(f"c) status=idle should be ok regardless of ts, got {r['status']}")
    return fails


def case_d_running_fresh() -> list[str]:
    fails = []
    r = with_repo_override({"status": "running", "step": "doing X", "ts": int(time.time()) - 30})
    if r["status"] != "ok":
        fails.append(f"d) running with fresh ts should be ok, got {r['status']} ({r['detail']})")
    return fails


def case_e_running_stale() -> list[str]:
    fails = []
    # 5 days old — exactly the failure mode observed.
    r = with_repo_override({"status": "running", "step": "Pass 361: starting", "ts": int(time.time()) - 5 * 86400})
    if r["status"] != "warn":
        fails.append(f"e) running with 5-day-old ts should be warn, got {r['status']} ({r['detail']})")
    if r["status"] == "warn" and "Pass 361" not in r["detail"]:
        fails.append(f"e) detail should mention the stuck step, got: {r['detail']}")
    return fails


def case_f_running_no_ts() -> list[str]:
    fails = []
    # No ts at all — can't determine staleness, must not false-alarm.
    r = with_repo_override({"status": "running", "step": "doing X"})
    if r["status"] != "ok":
        fails.append(f"f) running with no ts should be ok (defensive), got {r['status']}")
    return fails


# ---------------------------------------------------------------------------
# Task queue pileup — check_task_queue
# ---------------------------------------------------------------------------

def with_tasks_override(setup_fn):
    """Run check_task_queue against a temp WORKSPACE_DIR/tasks. setup_fn(tasks_dir)
    populates the queue. (Was REPO_DIR pre-#762; check_task_queue now resolves
    against the workspace because that's where the watcher reads from — see
    health-check.py top-of-file comment for the drift class.)"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        tasks_dir = td / "tasks"
        if setup_fn is not None:
            tasks_dir.mkdir(parents=True, exist_ok=True)
            setup_fn(tasks_dir)
        orig = hc.WORKSPACE_DIR
        try:
            hc.WORKSPACE_DIR = td
            return hc.check_task_queue(threshold_count=3, threshold_age_sec=300)
        finally:
            hc.WORKSPACE_DIR = orig


def case_g_no_tasks_dir() -> list[str]:
    fails = []
    r = with_tasks_override(None)
    if r["status"] != "ok":
        fails.append(f"g) missing tasks dir should be ok, got {r['status']}")
    return fails


def case_h_empty_queue() -> list[str]:
    fails = []
    r = with_tasks_override(lambda d: None)
    if r["status"] != "ok":
        fails.append(f"h) empty queue should be ok, got {r['status']}")
    return fails


def case_i_small_fresh_queue() -> list[str]:
    fails = []
    def setup(d):
        for i in range(2):
            write_task(d, f"task-{i}.txt", age_sec=10)
    r = with_tasks_override(setup)
    if r["status"] != "ok":
        fails.append(f"i) 2 fresh tasks should be ok, got {r['status']}")
    return fails


def case_j0_one_task_stuck_long() -> list[str]:
    """A SINGLE task stuck far past the age threshold must warn. The count and
    age conditions were ANDed, so 1-3 aged tasks were unreachable and green."""
    fails = []
    def setup(d):
        write_task(d, "task-stuck.txt", age_sec=3600)
    r = with_tasks_override(setup)
    if r["status"] != "warn":
        fails.append(
            f"j0) 1 task aged 3600s should warn, got {r['status']} ({r['detail']})")
    return fails


def case_j_large_fresh_queue() -> list[str]:
    fails = []
    # Many tasks but all fresh — count alone shouldn't alarm.
    def setup(d):
        for i in range(10):
            write_task(d, f"task-{i}.txt", age_sec=30)
    r = with_tasks_override(setup)
    if r["status"] != "ok":
        fails.append(f"j) 10 fresh tasks should be ok (heavy use is normal), got {r['status']}")
    return fails


def case_k_small_old_queue() -> list[str]:
    fails = []
    # Age alone shouldn't alarm BELOW the bar: 600s is under stuck_age_sec
    # (900), which is what separates this from j0 where age alone must warn.
    def setup(d):
        for i in range(2):
            write_task(d, f"task-{i}.txt", age_sec=600)
    r = with_tasks_override(setup)
    if r["status"] != "ok":
        fails.append(f"k) 2 old tasks should be ok (age alone), got {r['status']}")
    return fails


def case_l_pileup() -> list[str]:
    fails = []
    # The actual failure mode: many tasks, oldest is old. Both signals
    # together → warn.
    def setup(d):
        for i in range(5):
            write_task(d, f"task-{i}.txt", age_sec=600)
    r = with_tasks_override(setup)
    if r["status"] != "warn":
        fails.append(f"l) 5 old tasks should be warn (the actual incident), got {r['status']} ({r['detail']})")
    return fails


def case_m_archive_excluded() -> list[str]:
    fails = []
    # Archive subdir at tasks/archive/2026-05/ shouldn't count toward queue
    # (tasks/*.txt is non-recursive). Many archived tasks + small fresh queue
    # = ok.
    def setup(d):
        archive = d / "archive" / "2026-05"
        archive.mkdir(parents=True, exist_ok=True)
        for i in range(20):
            (archive / f"task-{i}.txt").write_text("archived")
        # And one fresh queue file.
        write_task(d, "task-current.txt", age_sec=10)
    r = with_tasks_override(setup)
    if r["status"] != "ok":
        fails.append(f"m) archived files leaked into count: {r['detail']}")
    return fails


def case_m2_completed_tasks_excluded() -> list[str]:
    fails = []
    def setup(d):
        results = d.parent / "results"
        results.mkdir()
        for i in range(8):
            task = write_task(d, f"task-{i}.txt", age_sec=600)
            (results / task.name).write_text("complete")
    r = with_tasks_override(setup)
    if r["status"] != "ok" or "empty" not in r["detail"]:
        fails.append(f"m2) completed tasks counted as pending: {r['detail']}")
    return fails


def case_m2b_retention_archived_tasks_excluded() -> list[str]:
    fails = []
    def setup(d):
        results = d.parent / "results"
        archive = results / "archive-2026-07-26"
        archive.mkdir(parents=True)
        # Non-file *.txt entries and non-directory archive-* entries are
        # ignored without affecting the completed-task set.
        (archive / "ignored.txt").mkdir()
        (results / "archive-not-a-directory").write_text("fixture")
        for i in range(8):
            task = write_task(d, f"task-{i}.txt", age_sec=600)
            (archive / task.name).write_text("complete")
    r = with_tasks_override(setup)
    if r["status"] != "ok" or "empty" not in r["detail"]:
        fails.append(f"m2b) retention-archived tasks counted as pending: {r['detail']}")
    return fails


def case_m3_completed_tasks_excluded_from_recovery() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tasks = root / "tasks"
        results = root / "results"
        tasks.mkdir()
        results.mkdir()
        task = write_task(tasks, "task-complete.txt", age_sec=600)
        (results / task.name).write_text("complete")
        if hc._oldest_pending_task(time.time(), tasks) is not None:
            fails.append("m3) completed task triggered core-recovery wedge detection")
    return fails


def case_m3b_archived_results_excluded_from_recovery() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tasks = root / "tasks"
        results = root / "results"
        archive = results / "archive" / "2026-07"
        tasks.mkdir()
        archive.mkdir(parents=True)
        task = write_task(tasks, "task-complete.txt", age_sec=600)
        (archive / task.name).write_text("complete")
        if hc._oldest_pending_task(time.time(), tasks) is not None:
            fails.append("m3b) archived result triggered core-recovery wedge detection")
    return fails


def case_m3c_gateway_archived_results_excluded_from_recovery() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tasks = root / "tasks"
        archive = root / "results" / "archive"
        tasks.mkdir()
        archive.mkdir(parents=True)
        task = write_task(tasks, "task-complete.txt", age_sec=600)
        (archive / "task-complete-1784690000.txt").write_text("complete")
        if hc._oldest_pending_task(time.time(), tasks) is not None:
            fails.append("m3c) gateway-archived result triggered recovery detection")
    return fails


def case_m3d_retention_archived_results_excluded_from_recovery() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        tasks = root / "tasks"
        archive = root / "results" / "archive-2026-07-26"
        tasks.mkdir()
        archive.mkdir(parents=True)
        task = write_task(tasks, "task-complete.txt", age_sec=600)
        (archive / "task-complete-1784690000.txt").write_text("complete")
        if hc._oldest_pending_task(time.time(), tasks) is not None:
            fails.append("m3d) retention-archived result triggered recovery detection")
    return fails


def case_m4_task_scan_error_fails_open() -> list[str]:
    fails = []
    with mock.patch.object(Path, "glob", side_effect=OSError("scan failed")):
        pending = hc._pending_task_files(Path("/unreadable/tasks"))
    if pending != []:
        fails.append(f"m4) task scan error should return an empty queue, got {pending}")
    return fails


# ---------------------------------------------------------------------------
# Notify-on-fail dedup — notify_for_failures
# ---------------------------------------------------------------------------

def case_n_notify_empty_no_call() -> list[str]:
    fails = []
    calls = []
    with tempfile.TemporaryDirectory() as td:
        state_file = Path(td) / "state" / "health-last-notified.json"
        hc.notify_for_failures(
            make_checks(("ok", "svcA"), ("ok", "svcB")),
            state_file=state_file,
            notify_cmd=["true"],  # would always succeed if called, but spy below
        )
        # No-failure path returns before invoking notify_cmd. Easier to spy
        # via state file existence — empty failures must not write state.
        if state_file.exists():
            fails.append("n) all-ok input wrote state file (should not)")
    return fails


def case_o_dedup_within_cooldown() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        state_file = Path(td) / "state" / "health-last-notified.json"
        checks = make_checks(("down", "voice-agent"))
        # Use `true` as a no-op stand-in so we don't fire real macOS notifs.
        hc.notify_for_failures(checks, state_file=state_file, notify_cmd=["true"])
        first = json.loads(state_file.read_text())
        # Second call with the same set, no time advance — must be deduped.
        hc.notify_for_failures(checks, state_file=state_file, notify_cmd=["true"])
        second = json.loads(state_file.read_text())
        if first != second:
            fails.append("o) within-cooldown second call updated state (should be no-op)")
        # +1 for the `_last_hash` sentinel (tracks the most-recently-alerted
        # hash so an unchanged failure set never re-fires on a timer).
        if len(first) != 2:
            fails.append(f"o) first call should write 1 history entry + sentinel, got {len(first)}")
    return fails


def case_p_different_sets_both_fire() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        state_file = Path(td) / "state" / "health-last-notified.json"
        hc.notify_for_failures(
            make_checks(("down", "voice-agent")),
            state_file=state_file, notify_cmd=["true"],
        )
        hc.notify_for_failures(
            make_checks(("down", "discord-bridge")),
            state_file=state_file, notify_cmd=["true"],
        )
        history = json.loads(state_file.read_text())
        # +1 for the `_last_hash` sentinel.
        if len(history) != 3:
            fails.append(f"p) two different sets should produce 2 history entries + sentinel, got {len(history)}")
    return fails


def case_q_separate_state_from_emit() -> list[str]:
    """notify and emit_task must use DIFFERENT default state files so they
    can't mutually suppress each other. Verifies that notify_for_failures
    writes its own state file under state/health-last-notified.json and not
    the emit-task path state/health-last-alerted.json."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        orig = hc.WORKSPACE_DIR
        try:
            hc.WORKSPACE_DIR = td
            hc.notify_for_failures(make_checks(("down", "svc")), notify_cmd=["true"])
        finally:
            hc.WORKSPACE_DIR = orig
        notified = td / "state" / "health-last-notified.json"
        alerted = td / "state" / "health-last-alerted.json"
        if not notified.exists():
            fails.append("q) notify_for_failures default state file not at state/health-last-notified.json")
        if alerted.exists():
            fails.append("q) notify_for_failures wrote to emit-task's state file (must be separate)")
    return fails


def case_r_same_set_past_cooldown_does_not_renotify() -> list[str]:
    """Regression (owner complaint 2026-07-01): a persistent, unchanged
    failure set must not re-fire the macOS notification just because time
    has passed. Seeds state as if last notified 25h ago (past the old 1h
    cooldown) and confirms a second call with the SAME set is silent —
    verified by checking the notify_cmd subprocess is never invoked."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        state_file = Path(td) / "state" / "health-last-notified.json"
        checks = make_checks(("down", "voice-agent"))
        set_key = "|".join(sorted(c["name"] for c in checks))
        hash_key = hc.hashlib.sha256(set_key.encode()).hexdigest()[:16]
        now_ms = int(time.time() * 1000)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            hash_key: now_ms - (25 * 3600 * 1000),
            "_last_hash": hash_key,
        }))
        # `false` marker file — if invoked, its mtime moves; we instead use a
        # command that would raise if actually run via a bogus executable,
        # proving suppression by absence of any exception/side effect.
        marker = Path(td) / "notify-fired"
        hc.notify_for_failures(
            checks, state_file=state_file,
            notify_cmd=["bash", "-c", f"touch {marker}"],
        )
        if marker.exists():
            fails.append("r) unchanged failure set re-notified after 25h — spam bug regressed")
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("a", case_a_status_missing),
        ("b", case_b_status_malformed),
        ("c", case_c_status_not_running),
        ("d", case_d_running_fresh),
        ("e", case_e_running_stale),
        ("f", case_f_running_no_ts),
        ("g", case_g_no_tasks_dir),
        ("h", case_h_empty_queue),
        ("i", case_i_small_fresh_queue),
        ("j", case_j_large_fresh_queue),
        ("k", case_k_small_old_queue),
        ("j0", case_j0_one_task_stuck_long),
        ("l", case_l_pileup),
        ("m", case_m_archive_excluded),
        ("m2", case_m2_completed_tasks_excluded),
        ("m2b", case_m2b_retention_archived_tasks_excluded),
        ("m3", case_m3_completed_tasks_excluded_from_recovery),
        ("m3b", case_m3b_archived_results_excluded_from_recovery),
        ("m3c", case_m3c_gateway_archived_results_excluded_from_recovery),
        ("m3d", case_m3d_retention_archived_results_excluded_from_recovery),
        ("m4", case_m4_task_scan_error_fails_open),
        ("n", case_n_notify_empty_no_call),
        ("o", case_o_dedup_within_cooldown),
        ("p", case_p_different_sets_both_fire),
        ("q", case_q_separate_state_from_emit),
        ("r", case_r_same_set_past_cooldown_does_not_renotify),
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
    print("\nAll stuck-loop / queue-pileup invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
