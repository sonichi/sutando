#!/usr/bin/env python3
"""
Tests for skills/learned-skills/scripts/detect-learned-skills.py — gate-guarded
workflow pattern miner.

Covers:
  a) Gate-off path: sentinel absent → exits cleanly, prints one-line message
  b) Gate-on path + empty archive → "no archived tasks found"
  c) Gate-on path + patterns below threshold → "no pattern appeared ≥N times"
  d) Gate-on path + 3+ identical patterns → prints candidate skill proposals
  e) CANCEL_INSTRUCTION and [deduped] tasks are excluded from pattern mining
  f) _normalize strips URLs and numbers for comparison

Run: python3 tests/detect-learned-skills.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import io
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "detect_learned_skills",
    REPO / "skills" / "learned-skills" / "scripts" / "detect-learned-skills.py",
)
dls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dls)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_task(tasks_dir: Path, name: str, task_body: str) -> None:
    p = tasks_dir / name
    p.write_text(f"id: {name}\ntask: {task_body}\n")


def _capture(fn, *args, **kwargs) -> str:
    """Run fn and return everything it printed to stdout."""
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _patch_workspace(td: Path) -> dict:
    """Override module-level path constants; returns dict for restore."""
    orig = {
        "WORKSPACE": dls.WORKSPACE,
        "STATE_DIR": dls.STATE_DIR,
        "TASKS_DIR": dls.TASKS_DIR,
        "SENTINEL": dls.SENTINEL,
    }
    dls.WORKSPACE = td
    dls.STATE_DIR = td / "state"
    dls.TASKS_DIR = td / "tasks"
    dls.SENTINEL = td / "state" / "learned-skills-enabled.sentinel"
    return orig


def _restore_workspace(orig: dict) -> None:
    for k, v in orig.items():
        setattr(dls, k, v)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def case_a_gate_off():
    """Sentinel absent → exits with zero, emits single-line gate-off message."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        orig = _patch_workspace(td)
        try:
            out = _capture(dls.main)
        finally:
            _restore_workspace(orig)
    if "gate off" not in out.lower() and "sentinel" not in out.lower() and "skipping" not in out.lower():
        fails.append(f"a) gate-off output should mention gate/sentinel/skipping, got: {out!r}")
    # Must be a single short line — not a multi-line proposal block
    non_empty = [l for l in out.splitlines() if l.strip()]
    if len(non_empty) > 2:
        fails.append(f"a) gate-off should emit ≤2 lines, got {len(non_empty)}: {out!r}")
    return fails


def case_b_gate_on_empty_archive():
    """Sentinel present, no archived tasks → 'no archived tasks found'."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "state").mkdir()
        (td / "state" / "learned-skills-enabled.sentinel").touch()
        orig = _patch_workspace(td)
        try:
            out = _capture(dls.main)
        finally:
            _restore_workspace(orig)
    if "no archived tasks" not in out.lower():
        fails.append(f"b) empty archive should report 'no archived tasks', got: {out!r}")
    return fails


def case_c_gate_on_below_threshold():
    """Tasks exist but each pattern appears < MIN_OCCURRENCES → no proposals."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "state").mkdir()
        (td / "state" / "learned-skills-enabled.sentinel").touch()
        archive = td / "tasks" / "archive"
        archive.mkdir(parents=True)
        # 2 tasks with the same pattern (threshold is 3)
        for i in range(2):
            _write_task(archive, f"task-{i}.txt", "search github for issues")
        orig = _patch_workspace(td)
        try:
            out = _capture(dls.main)
        finally:
            _restore_workspace(orig)
    if "no pattern" not in out.lower() and "candidate" not in out.lower():
        # Either "no pattern appeared >= N times" or possibly 0 candidates
        # as long as no proposal block is printed
        if "suggested skill" in out.lower():
            fails.append(f"c) below-threshold tasks should produce no proposals, got: {out!r}")
    return fails


def case_d_gate_on_with_candidates():
    """3+ tasks sharing a leading verb phrase → proposal block is printed."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "state").mkdir()
        (td / "state" / "learned-skills-enabled.sentinel").touch()
        archive = td / "tasks" / "archive"
        archive.mkdir(parents=True)
        for i in range(4):
            _write_task(archive, f"task-{i}.txt", f"search github for issues in repo {i}")
        orig = _patch_workspace(td)
        try:
            out = _capture(dls.main)
        finally:
            _restore_workspace(orig)
    if "candidate" not in out.lower() and "suggested skill" not in out.lower():
        fails.append(f"d) 4 matching tasks should produce a candidate proposal, got: {out!r}")
    return fails


def case_e_cancel_and_dedup_excluded():
    """CANCEL_INSTRUCTION and [deduped] task bodies are not counted."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "state").mkdir()
        (td / "state" / "learned-skills-enabled.sentinel").touch()
        archive = td / "tasks" / "processed"
        archive.mkdir(parents=True)
        # 5 CANCEL_INSTRUCTION tasks — should all be excluded
        for i in range(5):
            _write_task(archive, f"task-cancel-{i}.txt", f"CANCEL_INSTRUCTION: cancel task-{i}")
        # 5 deduped tasks — should also be excluded
        for i in range(5):
            _write_task(archive, f"task-dedup-{i}.txt", f"[deduped: task-{i}]")
        # 1 real task — not enough to hit threshold
        _write_task(archive, "task-real.txt", "check the weather today")
        orig = _patch_workspace(td)
        try:
            out = _capture(dls.main)
        finally:
            _restore_workspace(orig)
    if "suggested skill" in out.lower():
        fails.append(f"e) cancel/dedup tasks should not count toward threshold, got: {out!r}")
    return fails


def case_f_normalize_strips_ids():
    """_normalize should treat 'check issue 123' and 'check issue 456' as the same pattern."""
    fails = []
    n1 = dls._normalize("search github for issues in repo 123")
    n2 = dls._normalize("search github for issues in repo 456")
    if n1 != n2:
        fails.append(f"f) normalized forms should match after number stripping: {n1!r} != {n2!r}")
    # URLs should also be stripped
    n3 = dls._normalize("open https://github.com/foo/bar and check")
    n4 = dls._normalize("open https://github.com/baz/qux and check")
    if n3 != n4:
        fails.append(f"f) normalized forms should match after URL stripping: {n3!r} != {n4!r}")
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("a", case_a_gate_off),
        ("b", case_b_gate_on_empty_archive),
        ("c", case_c_gate_on_below_threshold),
        ("d", case_d_gate_on_with_candidates),
        ("e", case_e_cancel_and_dedup_excluded),
        ("f", case_f_normalize_strips_ids),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  FAIL case {label}")
            for f in fails:
                print(f"    {f}")
        else:
            print(f"  PASS case {label}")

    total = len(cases)
    failed = len(all_failures)
    print(f"\nResults: {total - failed}/{total} passed")
    return 1 if all_failures else 0


if __name__ == "__main__":
    sys.exit(main())
