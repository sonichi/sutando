#!/usr/bin/env python3
"""Unit tests for src/cron_ownership.py — the shared owner filter that keeps
the core's /schedule-crons registration pass and a worker's startup
registration pass from both claiming the same crons.json entry."""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "cron_ownership", REPO / "src" / "cron_ownership.py")
co = importlib.util.module_from_spec(spec)
spec.loader.exec_module(co)

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {extra}")
        FAILURES.append(name)


def main() -> int:
    core_entry = {"name": "main-loop", "cron": "*/15 * * * *", "prompt_skill": "proactive-loop"}
    worker_entry = {"name": "event-mining-hourly", "cron": "7 * * * *", "prompt": "...",
                     "owner": "d1fc9b10050b490d894947f96627447f"}
    other_worker_entry = {"name": "alltime-weekly", "cron": "23 5 * * 0", "prompt": "...",
                           "owner": "39c926c63a2c4943acf57dde3eac7719"}
    blank_owner_entry = {"name": "triage-scan-fresh", "cron": "*/15 * * * *", "prompt": "...",
                          "owner": ""}
    bad_owner_entry = {"name": "weird", "cron": "0 0 * * *", "prompt": "...", "owner": 42}

    # ── entry_owner ──────────────────────────────────────────────────────
    check("no owner field defaults to core", co.entry_owner(core_entry) == co.CORE)
    check("explicit worker owner is returned as-is",
          co.entry_owner(worker_entry) == "d1fc9b10050b490d894947f96627447f")
    check("blank string owner falls back to core", co.entry_owner(blank_owner_entry) == co.CORE)
    check("non-string owner falls back to core", co.entry_owner(bad_owner_entry) == co.CORE)

    # ── entries_for_owner ────────────────────────────────────────────────
    entries = [core_entry, worker_entry, other_worker_entry, blank_owner_entry, bad_owner_entry]

    core_view = co.entries_for_owner(entries, co.CORE)
    check("core sees only unowned/blank/malformed entries",
          core_view == [core_entry, blank_owner_entry, bad_owner_entry], core_view)
    check("core does NOT see either worker's pinned entry",
          worker_entry not in core_view and other_worker_entry not in core_view)

    worker_view = co.entries_for_owner(entries, "d1fc9b10050b490d894947f96627447f")
    check("a worker sees only its own pinned entry", worker_view == [worker_entry], worker_view)
    check("a worker does not see the other worker's entry", other_worker_entry not in worker_view)
    check("a worker does not see core-owned entries", core_entry not in worker_view)

    unknown_worker_view = co.entries_for_owner(entries, "0000000000000000000000000000000")
    check("a worker id owning nothing gets an empty list", unknown_worker_view == [])

    check("order is preserved, not resorted",
          co.entries_for_owner([other_worker_entry, worker_entry], "d1fc9b10050b490d894947f96627447f")
          == [worker_entry])

    check("empty entry list is fine", co.entries_for_owner([], co.CORE) == [])

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
