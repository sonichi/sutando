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


def write_status(repo_dir: Path, payload: dict | None) -> None:
    """Write core-status.json into a temp REPO_DIR override; pass None to
    skip writing the file at all."""
    if payload is not None:
        (repo_dir / "core-status.json").write_text(json.dumps(payload))


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
    """Run check_core_proactive_loop against a temp REPO_DIR. Returns the
    check dict. `payload=None` means don't write the status file."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        write_status(td, payload)
        orig_repo = hc.REPO_DIR
        try:
            hc.REPO_DIR = td
            return hc.check_core_proactive_loop(threshold_sec=600)
        finally:
            hc.REPO_DIR = orig_repo


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
        orig = hc.REPO_DIR
        try:
            hc.REPO_DIR = td
            r = hc.check_core_proactive_loop(threshold_sec=600)
        finally:
            hc.REPO_DIR = orig
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
    """Run check_task_queue against a temp REPO_DIR/tasks. setup_fn(tasks_dir)
    populates the queue."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        tasks_dir = td / "tasks"
        if setup_fn is not None:
            tasks_dir.mkdir(parents=True, exist_ok=True)
            setup_fn(tasks_dir)
        orig = hc.REPO_DIR
        try:
            hc.REPO_DIR = td
            return hc.check_task_queue(threshold_count=3, threshold_age_sec=300)
        finally:
            hc.REPO_DIR = orig


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
    # Small queue but old — age alone shouldn't alarm. The user might have
    # 1-2 long-tail tasks that take a while; not stuck.
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
        if len(first) != 1:
            fails.append(f"o) first call should write 1 history entry, got {len(first)}")
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
        if len(history) != 2:
            fails.append(f"p) two different sets should produce 2 history entries, got {len(history)}")
    return fails


def case_q_separate_state_from_emit() -> list[str]:
    """notify and emit_task must use DIFFERENT default state files so they
    can't mutually suppress each other. Verifies that notify_for_failures
    writes its own state file under state/health-last-notified.json and not
    the emit-task path state/health-last-alerted.json."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        orig = hc.REPO_DIR
        try:
            hc.REPO_DIR = td
            hc.notify_for_failures(make_checks(("down", "svc")), notify_cmd=["true"])
        finally:
            hc.REPO_DIR = orig
        notified = td / "state" / "health-last-notified.json"
        alerted = td / "state" / "health-last-alerted.json"
        if not notified.exists():
            fails.append("q) notify_for_failures default state file not at state/health-last-notified.json")
        if alerted.exists():
            fails.append("q) notify_for_failures wrote to emit-task's state file (must be separate)")
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
        ("l", case_l_pileup),
        ("m", case_m_archive_excluded),
        ("n", case_n_notify_empty_no_call),
        ("o", case_o_dedup_within_cooldown),
        ("p", case_p_different_sets_both_fire),
        ("q", case_q_separate_state_from_emit),
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
