#!/usr/bin/env python3
"""
Tests that `sandbox-delegation` reports guest/team tasks that received the
fallback sentinel instead of an answer.

`codex-presence` resolves the binary on PATH and stops there. That is presence,
not drivability: it stayed `ok` for two days while every `codex exec` exited 1
on a model pin the installed CLI rejected, so every non-owner sender got
"Sandbox unavailable (codex exit 1) — no reply generated." and nobody was told.
The sentinel goes to the sender, never to the owner, so a low-volume path can
fail indefinitely with no pile-up to notice.

This probe keys on the sentinel rather than on any one cause, so a pin, a lost
login, a quota wall and a wiped binary all surface the same way.

Covers:
  a) recent sentinel in results/          → warn, and the task id is named
  b) NO sentinel                          → ok            (control for a)
  c) sentinel OLDER than the window       → ok            (control for the window)
  d) sentinel only in results/archive/<m>/→ warn          (the drained case)
  e) unscannable results/archive/         → warn, not a silent ok
  f) a non-task-* file carrying the text  → ok            (scope is task results)

Run: python3 tests/health-check-sandbox-delegation.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

SENTINEL = "Sandbox unavailable (codex exit 1) — no reply generated.\n"


def _run(tmp: Path, **kw) -> dict:
    """Call the SHIPPED probe with WORKSPACE_DIR pointed at a fixture."""
    orig = hc.WORKSPACE_DIR
    hc.WORKSPACE_DIR = tmp
    try:
        return hc.check_sandbox_delegation(**kw)
    finally:
        hc.WORKSPACE_DIR = orig


def _results(tmp: Path) -> Path:
    d = tmp / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def case_a_recent_sentinel_warns() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (_results(tmp) / "task-guest-1.txt").write_text(SENTINEL, encoding="utf-8")
        r = _run(tmp)
        if r["status"] != "warn":
            fails.append(f"(a) recent sentinel: expected warn, got {r['status']} — {r['detail']}")
        if "task-guest-1.txt" not in r["detail"]:
            fails.append(f"(a) detail does not name the task: {r['detail']}")
    return fails


def case_b_no_sentinel_is_ok() -> list[str]:
    """Control for (a): the warn must be caused by the sentinel, not by any
    task result existing at all."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (_results(tmp) / "task-guest-1.txt").write_text("a perfectly normal reply\n", encoding="utf-8")
        r = _run(tmp)
        if r["status"] != "ok":
            fails.append(f"(b) no sentinel: expected ok, got {r['status']} — {r['detail']}")
    return fails


def case_c_old_sentinel_is_ok() -> list[str]:
    """Control for the window: an outage fixed last week must not warn forever."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = _results(tmp) / "task-guest-old.txt"
        p.write_text(SENTINEL, encoding="utf-8")
        old = time.time() - 90000  # > the 86400s default window
        os.utime(p, (old, old))
        r = _run(tmp)
        if r["status"] != "ok":
            fails.append(f"(c) stale sentinel: expected ok, got {r['status']} — {r['detail']}")
        # and it must still fire when the window is widened to include it
        r2 = _run(tmp, window_sec=180000)
        if r2["status"] != "warn":
            fails.append("(c) widening the window did not surface the same file — "
                         f"got {r2['status']}, so (c) proves nothing about the window")
    return fails


def case_d_archived_sentinel_warns() -> list[str]:
    """results/ is drained in under a second, so the evidence is normally in the
    month-partitioned archive. Scanning only results/ reads a live outage clean."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        arch = _results(tmp) / "archive" / "2026-09"
        arch.mkdir(parents=True)
        (arch / "task-guest-2.txt").write_text(SENTINEL, encoding="utf-8")
        r = _run(tmp)
        if r["status"] != "warn":
            fails.append(f"(d) archived sentinel: expected warn, got {r['status']} — {r['detail']}")
    return fails


def case_d2_flat_archive_sentinel_warns() -> list[str]:
    """A sentinel sitting DIRECTLY in results/archive/, not under a month dir.

    Most recent results land here, not in archive/<month>/: measured 273 of 284
    task-*.txt in the last 24h on a live host. `roots` listed archive's
    SUBDIRECTORIES, so a flat file was scanned by nothing and a live outage read
    clean. Case (d) does not cover this — it passes with or without the fix."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        arch = _results(tmp) / "archive"
        arch.mkdir(parents=True)
        (arch / "task-guest-3.txt").write_text(SENTINEL, encoding="utf-8")
        r = _run(tmp)
        if r["status"] != "warn":
            fails.append(f"(d2) flat-archive sentinel: expected warn, got {r['status']} — {r['detail']}")
    return fails


def case_e_unscannable_archive_warns() -> list[str]:
    """An unreadable directory must not read as an absence of failures.

    Injected at os.scandir rather than staged with chmod 0o000: CI runs as root,
    where mode bits are ignored and the branch is never entered — a test that
    skips under the uid CI uses is a branch CI has never seen."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        arch = _results(tmp) / "archive" / "2026-09"
        arch.mkdir(parents=True)
        real = hc.os.scandir

        # Two scandir sites, two branches: enumerating archive/ itself, and
        # walking each root. Deny each in turn so both error paths are proven.
        for deny_suffix, label in (("archive", "archive/ enumeration"), ("2026-09", "a month root")):
            def denied(path, *a, **k):
                if str(path).endswith(deny_suffix):
                    raise PermissionError(13, "Permission denied", str(path))
                return real(path, *a, **k)

            hc.os.scandir = denied
            try:
                r = _run(tmp)
            finally:
                hc.os.scandir = real
            if r["status"] != "warn":
                fails.append(f"(e) unscannable {label}: expected warn, got {r['status']} — {r['detail']}")
            if "could not scan" not in r["detail"]:
                fails.append(f"(e) {label}: the warn must say it could not scan, got: {r['detail']}")
    return fails


def case_e2_unreadable_file_is_isolated() -> list[str]:
    """One unreadable entry must not decide the answer for the directory — and
    must be counted, so the report says how much it could not see."""
    fails = []
    real_open = hc.open if hasattr(hc, "open") else open
    import builtins
    orig = builtins.open

    def flaky(path, *a, **k):
        if str(path).endswith("task-guest-locked.txt"):
            raise PermissionError(13, "Permission denied", str(path))
        return orig(path, *a, **k)

    # (i) unreadable beside a real sentinel: still warns, and counts the unreadable one
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (_results(tmp) / "task-guest-locked.txt").write_text("x", encoding="utf-8")
        (_results(tmp) / "task-guest-9.txt").write_text(SENTINEL, encoding="utf-8")
        builtins.open = flaky
        try:
            r = _run(tmp)
        finally:
            builtins.open = orig
        if r["status"] != "warn":
            fails.append(f"(e2-i) sentinel + unreadable: expected warn, got {r['status']}")
        if "1 result file(s) unreadable" not in r["detail"]:
            fails.append(f"(e2-i) the unreadable count is missing from: {r['detail']}")
    # (ii) unreadable alone: ok, but the ok names what it could not read
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (_results(tmp) / "task-guest-locked.txt").write_text("x", encoding="utf-8")
        builtins.open = flaky
        try:
            r = _run(tmp)
        finally:
            builtins.open = orig
        if r["status"] != "ok":
            fails.append(f"(e2-ii) unreadable alone: expected ok, got {r['status']}")
        if "1 result file(s) unreadable" not in r["detail"]:
            fails.append(f"(e2-ii) an ok that hides an unreadable file is a silent gap: {r['detail']}")
    return fails


def case_f_non_task_file_ignored() -> list[str]:
    """Scope is task results. A note or a proactive body quoting the sentence is
    not a sender who went unanswered."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (_results(tmp) / "proactive-note.txt").write_text(SENTINEL, encoding="utf-8")
        r = _run(tmp)
        if r["status"] != "ok":
            fails.append(f"(f) non-task file: expected ok, got {r['status']} — {r['detail']}")
    return fails


def main() -> int:
    fails: list[str] = []
    for fn in (case_a_recent_sentinel_warns, case_b_no_sentinel_is_ok,
               case_c_old_sentinel_is_ok, case_d_archived_sentinel_warns,
               case_d2_flat_archive_sentinel_warns,
               case_e_unscannable_archive_warns, case_e2_unreadable_file_is_isolated,
               case_f_non_task_file_ignored):
        fails += fn()
    if fails:
        print("FAIL")
        for f in fails:
            print("  " + f)
        return 1
    print("PASS sandbox-delegation: warns on a recent sentinel (results/, archive/ flat and archive/<month>/), "
          "ok without one, ok outside the window, warn on an unscannable dir, "
          "unreadable files counted not fatal, task-* only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
