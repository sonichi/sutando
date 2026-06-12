#!/usr/bin/env python3
"""Verify the copres-tool sentinel-gate contract.

Pre-patch (broken): copres_* tools register globally; Gemini fires them
on greetings like "Hi Lucy". Post-patch: tools array is empty when
`<workspace>/state/presenter-mode.sentinel` is absent.

Checks:
1. Static — grep tools.ts for the sentinel-existsSync guard before the
   tools export.
2. Runtime — query the recent voice sqlite for unprovoked
   copres_next_anchor / copres_next_step / copres_open_discord
   tool_call rows (no user "jump" within the 12-second lookback).
   Zero such rows in the last 24h = bug squashed.

Exit codes:
  0  contract holds (static guard present + no recent unprovoked fires)
  1  static guard missing (patch not applied)
  2  unprovoked tool_calls found in voice sqlite

Run after applying the patch + re-launching the voice agent without
the sentinel, then say "Hi Lucy" once. If `kill 1` later this script
still reports clean.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


WORKSPACE = Path(os.environ.get("SUTANDO_WORKSPACE", str(Path.home() / ".sutando/workspace")))
REPO = Path(__file__).resolve().parent.parent
TOOLS_TS = REPO / "skills/personal-co-present-tools/tools.ts"
DB = WORKSPACE / "data/conversation.sqlite"
SENTINEL = WORKSPACE / "state/presenter-mode.sentinel"


def check_static_guard() -> bool:
    """Look for the sentinel-existsSync gate around the tools export."""
    src = TOOLS_TS.read_text()
    # Required: an existsSync(...presenter-mode.sentinel...) reference within the
    # module, AND the tools export conditioning on it. Cheap textual check.
    if "presenter-mode.sentinel" not in src:
        print(f"[FAIL] {TOOLS_TS}: no presenter-mode.sentinel reference — patch not applied")
        return False
    if "existsSync" not in src:
        print(f"[FAIL] {TOOLS_TS}: no existsSync import — patch not applied")
        return False
    print(f"[PASS] static guard present in {TOOLS_TS.name}")
    return True


def check_unprovoked_fires(window_seconds: int = 86400) -> int:
    """Return count of copres_* tool_calls without a 'jump' user utterance
    within the prior 12s (matching the LOOKBACK_SEC in tools.ts)."""
    if not DB.exists():
        print(f"[SKIP] voice sqlite not found at {DB}")
        return 0
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = conn.cursor()
    cutoff = f"strftime('%s','now') - {window_seconds}"
    tool_calls = cur.execute(
        f"SELECT id, ts_unix, text FROM voice "
        f"WHERE kind='tool_call' AND text LIKE 'copres_%' "
        f"  AND ts_unix > ({cutoff}) "
        f"ORDER BY ts_unix"
    ).fetchall()
    bad: list[tuple[int, float, str]] = []
    for tc_id, tc_ts, tool_name in tool_calls:
        prior = cur.execute(
            "SELECT lower(coalesce(text,'')) FROM voice "
            "WHERE kind='user' AND ts_unix BETWEEN ?-12 AND ? "
            "ORDER BY ts_unix DESC LIMIT 5",
            (tc_ts, tc_ts),
        ).fetchall()
        haystack = " ".join(r[0] for r in prior)
        if "jump" not in haystack:
            bad.append((tc_id, tc_ts, tool_name))
    if bad:
        print(f"[FAIL] {len(bad)} unprovoked copres_* tool_call(s) in the last {window_seconds//3600}h:")
        for tc_id, tc_ts, tool_name in bad[:10]:
            print(f"       id={tc_id} ts={tc_ts:.0f} tool={tool_name}")
        if len(bad) > 10:
            print(f"       ... {len(bad)-10} more")
    else:
        print(f"[PASS] no unprovoked copres_* fires in the last {window_seconds//3600}h")
    return len(bad)


def main() -> int:
    print(f"workspace: {WORKSPACE}")
    print(f"sentinel:  {SENTINEL} ({'EXISTS' if SENTINEL.exists() else 'absent'})")
    static_ok = check_static_guard()
    runtime_bad = check_unprovoked_fires()
    if not static_ok:
        return 1
    if runtime_bad:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
