#!/usr/bin/env python3
"""A stale VIEW of a peer must never be reported as a stopped peer.

The regression, with the real numbers that produced it (2026-08-02, Pro reading
Mini): Pro held a `restart-watch.json` committed at 10:40:21Z carrying
`heartbeat_at 10:29:57Z`, read it at 11:11Z against wall-clock, and reported the
peer's beat "stopped". Mini had beaten at 10:29 · 10:55 · 11:11 throughout.

Wall-clock said 41 min stale. The snapshot said 10 min — inside Mini's own
45-minute window. Same file, same instant, opposite verdicts, and only one of
them was about the peer.

The first case below is that exact scenario. It FAILS against any wall-clock
implementation, which is what makes it a regression test rather than a
restatement of the code.

Run:  python3 tests/peer-watch-verdict.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("peer_watch", REPO / "scripts" / "peer-watch.py")
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail}")


def T(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


BASE = {"state": "back", "came_back_at": "2026-08-02T05:04:22Z", "valid_for_minutes": 45}


def doc(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


print("peer-watch verdicts")

# --- 1. THE REGRESSION -------------------------------------------------------
# Wall-clock: 11:11 - 10:29:57 = 41 min -> "stale". Snapshot: 10:40:21 - 10:29:57
# = 10 min -> healthy. The peer was healthy. Only the second reading is about it.
v = pw.evaluate(doc(heartbeat_at="2026-08-02T10:29:57Z"),
                committed=T("2026-08-02T10:40:21Z"), now=T("2026-08-02T11:11:00Z"))
check("a stale VIEW of a beating peer is ALIVE_AS_OF, not a stopped beat",
      v["verdict"] == "ALIVE_AS_OF", str(v))
check("...and the stale snapshot is still REPORTED, not hidden",
      v.get("snapshot_age_min", 0) > 30, str(v))
check("...and the beat lag is measured against the snapshot (≈10 min, not ≈41)",
      9 <= v.get("beat_lag_min", 0) <= 11, str(v))

# --- 2. A genuinely stopped beat must still fire -----------------------------
# Same window, but the peer had already gone quiet BEFORE it published. No amount
# of transport lag can explain that, so it is the peer.
v = pw.evaluate(doc(heartbeat_at="2026-08-02T09:00:00Z"),
                committed=T("2026-08-02T10:40:21Z"), now=T("2026-08-02T10:41:00Z"))
check("a peer that stopped beating BEFORE publishing is BEAT_STOPPED",
      v["verdict"] == "BEAT_STOPPED", str(v))
check("...even when my snapshot is perfectly fresh",
      v.get("snapshot_age_min", 99) < 2, str(v))

# --- 3. A declared restart that never returned -------------------------------
# Protocol: this branch IGNORES freshness — a stopped heartbeat during a declared
# restart is the expected condition, not a reason to downgrade to UNKNOWN.
v = pw.evaluate(doc(state="down", came_back_at=None,
                    went_down_at="2026-08-02T04:59:10Z",
                    expected_back_by="2026-08-02T05:14:10Z",
                    heartbeat_at="2026-08-02T04:58:00Z"),
                committed=T("2026-08-02T04:59:00Z"), now=T("2026-08-02T05:40:00Z"))
check("down + past expected_back_by + no came_back_at is COMEBACK_FAILED",
      v["verdict"] == "COMEBACK_FAILED", str(v))

v = pw.evaluate(doc(state="down", came_back_at=None,
                    expected_back_by="2026-08-02T06:00:00Z",
                    heartbeat_at="2026-08-02T05:30:00Z"),
                committed=T("2026-08-02T05:31:00Z"), now=T("2026-08-02T05:40:00Z"))
check("down but still INSIDE expected_back_by does not escalate",
      v["verdict"] != "COMEBACK_FAILED", str(v))

# --- 4. Untracked file: unknown, never "alive" -------------------------------
v = pw.evaluate(doc(heartbeat_at="2026-08-02T10:29:57Z"), committed=None,
                now=T("2026-08-02T10:31:00Z"))
check("no commit time -> NOT_ARMED, never a healthy verdict",
      v["verdict"] == "NOT_ARMED", str(v))

# --- 5. Exit codes are the escalation contract -------------------------------
check("ALIVE_AS_OF exits 0",
      pw.evaluate(doc(heartbeat_at="2026-08-02T10:35:00Z"),
                  T("2026-08-02T10:40:00Z"), T("2026-08-02T12:00:00Z"))["exit"] == 0)
check("BEAT_STOPPED exits 2",
      pw.evaluate(doc(heartbeat_at="2026-08-02T09:00:00Z"),
                  T("2026-08-02T10:40:00Z"), T("2026-08-02T10:41:00Z"))["exit"] == 2)

# --- 6. Positive control -----------------------------------------------------
# Without this, every assertion above could pass against a stub that always
# returns ALIVE_AS_OF for one case and BEAT_STOPPED for another by accident.
verdicts = {
    pw.evaluate(doc(heartbeat_at="2026-08-02T10:29:57Z"),
                T("2026-08-02T10:40:21Z"), T("2026-08-02T11:11:00Z"))["verdict"],
    pw.evaluate(doc(heartbeat_at="2026-08-02T09:00:00Z"),
                T("2026-08-02T10:40:21Z"), T("2026-08-02T10:41:00Z"))["verdict"],
}
check("positive control: the two headline inputs produce DIFFERENT verdicts",
      len(verdicts) == 2, str(verdicts))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — a stale view reads as stale, a stopped peer reads as stopped")
