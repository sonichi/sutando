#!/usr/bin/env python3
"""
Regression tests for PR #607's `emit_task_for_failures()` dedup logic.

Cold review (Mini, 2026-05-05) flagged the missing test as a non-blocking
nit; this is the follow-up. Guards the four properties that motivated the
PR:

  a) empty failures   → no task file written
  b) same set < 1h    → cooldown suppresses duplicate task
  c) set changes      → new hash, new task file fires
  d) 24h pruning      → history file doesn't grow unboundedly

Plus:

  e) `warn` is in the failure predicate (32efa4d2 followup)
  f) hash covers the FULL sorted set, not first-member-only

Run: python3 tests/health-check-emit-task.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
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
    """[(status, name), ...] → list of check dicts."""
    return [{"name": n, "status": s, "detail": "test"} for (s, n) in statuses_and_names]


def list_task_files(tasks_dir: Path) -> list[Path]:
    return sorted(tasks_dir.glob("task-health-*.txt"))


def case_a_empty_failures_no_file() -> list[str]:
    """Empty failures → emit_task_for_failures returns without writing."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_file = td / "state" / "health-last-alerted.json"
        tasks_dir = td / "tasks"
        # No failures → empty list, no failures matched
        all_ok = make_checks(("ok", "svcA"), ("ok", "svcB"))
        hc.emit_task_for_failures(all_ok, state_file=state_file, tasks_dir=tasks_dir)
        if list_task_files(tasks_dir):
            fails.append("a) all-ok input wrote a task file (should not)")
        if state_file.exists():
            fails.append("a) all-ok input touched state_file (should not)")
    return fails


def case_b_same_hash_within_cooldown() -> list[str]:
    """Same failure set called twice → second call suppressed by 1h cooldown."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_file = td / "state" / "health-last-alerted.json"
        tasks_dir = td / "tasks"
        checks = make_checks(("down", "voice-agent"))
        hc.emit_task_for_failures(checks, state_file=state_file, tasks_dir=tasks_dir)
        first = list_task_files(tasks_dir)
        if len(first) != 1:
            fails.append(f"b) first call should write 1 task file, got {len(first)}")
        # Second call within cooldown — should NOT add another file.
        hc.emit_task_for_failures(checks, state_file=state_file, tasks_dir=tasks_dir)
        second = list_task_files(tasks_dir)
        if len(second) != 1:
            fails.append(f"b) within-cooldown second call wrote a duplicate (now {len(second)} files)")
    return fails


def case_c_different_set_emits() -> list[str]:
    """Different failure set → different hash → new task fires."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_file = td / "state" / "health-last-alerted.json"
        tasks_dir = td / "tasks"
        # First failure set.
        hc.emit_task_for_failures(
            make_checks(("down", "voice-agent")),
            state_file=state_file, tasks_dir=tasks_dir,
        )
        # Different set — one recovers, another fails.
        # Force at least 1s of clock advance so the task filename + body
        # differ (filename uses int(time.time()) → collision otherwise).
        time.sleep(1.05)
        hc.emit_task_for_failures(
            make_checks(("down", "discord-bridge"), ("warn", "telegram-bridge")),
            state_file=state_file, tasks_dir=tasks_dir,
        )
        files = list_task_files(tasks_dir)
        if len(files) != 2:
            fails.append(f"c) two different sets should produce 2 files, got {len(files)}")
    return fails


def case_d_history_pruned_after_24h() -> list[str]:
    """Entries older than 24h are pruned from state file."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_file = td / "state" / "health-last-alerted.json"
        tasks_dir = td / "tasks"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        # Seed state with one stale entry (25h old) + one fresh entry (10min old).
        now_ms = int(time.time() * 1000)
        stale_ts = now_ms - (25 * 3600 * 1000)
        fresh_ts = now_ms - (10 * 60 * 1000)
        state_file.write_text(json.dumps({
            "stale_hash_aaaaaaaa": stale_ts,
            "fresh_hash_bbbbbbbb": fresh_ts,
        }))
        # Trigger a new emit — function should prune stale and add new entry.
        hc.emit_task_for_failures(
            make_checks(("down", "new-failure")),
            state_file=state_file, tasks_dir=tasks_dir,
        )
        history = json.loads(state_file.read_text())
        if "stale_hash_aaaaaaaa" in history:
            fails.append("d) 25h-old entry was not pruned")
        if "fresh_hash_bbbbbbbb" not in history:
            fails.append("d) 10min-old entry was wrongly pruned")
        # New entry should also be present.
        if len(history) < 2:
            fails.append(f"d) new entry not added; history has {len(history)} keys")
    return fails


def case_e_warn_is_failure() -> list[str]:
    """`warn` status should trigger a task (motivating bug class for the PR)."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_file = td / "state" / "health-last-alerted.json"
        tasks_dir = td / "tasks"
        # Only `warn` failures — must still emit (per 32efa4d2 followup).
        hc.emit_task_for_failures(
            make_checks(("warn", "discord-bridge")),
            state_file=state_file, tasks_dir=tasks_dir,
        )
        if not list_task_files(tasks_dir):
            fails.append("e) `warn` failure produced no task — discord-bridge dead-log-inode bug class missed")
    return fails


def case_f_hash_covers_full_set() -> list[str]:
    """Hash MUST be over the sorted full set, not just the first member.

    If the hash were first-member-only, two sets that share their alphabetically-
    first member but differ otherwise would collide. We construct exactly that
    case and verify they produce different task counts (i.e. distinct hashes).
    """
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state_file = td / "state" / "health-last-alerted.json"
        tasks_dir = td / "tasks"
        # Both sets sort to "aaa-svc" first.
        hc.emit_task_for_failures(
            make_checks(("down", "aaa-svc"), ("down", "zzz-svc")),
            state_file=state_file, tasks_dir=tasks_dir,
        )
        time.sleep(1.05)
        hc.emit_task_for_failures(
            make_checks(("down", "aaa-svc"), ("down", "mmm-svc")),
            state_file=state_file, tasks_dir=tasks_dir,
        )
        files = list_task_files(tasks_dir)
        if len(files) != 2:
            fails.append(f"f) hash collided on shared first-element — should be 2 files, got {len(files)}")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_empty_failures_no_file),
        ("b", case_b_same_hash_within_cooldown),
        ("c", case_c_different_set_emits),
        ("d", case_d_history_pruned_after_24h),
        ("e", case_e_warn_is_failure),
        ("f", case_f_hash_covers_full_set),
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
    print("\nAll emit-task dedup invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
