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
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("peer_watch", REPO / "src" / "peer-watch.py")
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
# `now` must sit INSIDE the peer's declared window: this case asserts the exit
# code of a HEALTHY verdict, and 12:00 against a 10:35 beat is 40 min past the
# peer's own valid_until — correctly VIEW_STALE once staleness became a verdict.
# The fixture predates that rule; its `now` was arbitrary because age did not
# matter yet. Making it explicit rather than loosening the rule to fit it.
check("ALIVE_AS_OF exits 0",
      pw.evaluate(doc(heartbeat_at="2026-08-02T10:35:00Z"),
                  T("2026-08-02T10:40:00Z"), T("2026-08-02T11:00:00Z"))["exit"] == 0)
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

# --- 7. commit_time() against a REAL repo, and against mtime ------------------
# The whole design rests on commit time rather than mtime, so that choice needs a
# test that can tell them apart. Touching the file AFTER committing makes mtime
# newer than the commit; an implementation that reached for st_mtime would report
# the snapshot as fresher than it is -- the exact inversion, and invisible to
# every assertion above because those pass `committed` in directly.
import os
import subprocess
import tempfile

with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-08-02T10:40:21+00:00",
           "GIT_COMMITTER_DATE": "2026-08-02T10:40:21+00:00",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                    capture_output=True, text=True, env=env, timeout=60)
    run("init", "-q")
    rel = "hosts/PeerA/restart-watch.json"
    (repo / "hosts" / "PeerA").mkdir(parents=True)
    (repo / rel).write_text("{}")
    run("add", rel)
    run("commit", "-q", "-m", "add")

    got = pw.commit_time(repo, rel)
    check("commit_time reads the real commit date",
          got is not None and got.astimezone(dt.timezone.utc).strftime("%H:%M:%S") == "10:40:21",
          str(got))

    # Now make mtime newer than the commit — a sync/checkout does exactly this.
    os.utime(repo / rel, (2_000_000_000, 2_000_000_000))
    still = pw.commit_time(repo, rel)
    check("commit_time ignores mtime — a touched file still reports its COMMIT date",
          still == got, f"got {still}, expected {got}")

    check("commit_time on an untracked path is None, never a guess",
          pw.commit_time(repo, "hosts/PeerA/not-committed.json") is None)

with tempfile.TemporaryDirectory() as td2:
    check("commit_time outside a git repo is None",
          pw.commit_time(Path(td2), "anything.json") is None)

# --- 8. main(): the CLI contract --------------------------------------------
with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    rc = pw.main(["PeerMissing", "--workspace", str(ws)])
    check("main() exits 1 for a peer with no signal file", rc == 1)

    (ws / "hosts" / "PeerB").mkdir(parents=True)
    (ws / "hosts" / "PeerB" / "restart-watch.json").write_text(json.dumps(
        {"state": "back", "heartbeat_at": "2026-08-02T10:29:57Z", "valid_for_minutes": 45}))
    # Untracked (no repo) -> NOT_ARMED rather than a healthy verdict: a file we
    # cannot date is a file we cannot judge.
    rc = pw.main(["PeerB", "--workspace", str(ws)])
    check("main() on an undatable file exits 1, not 0", rc == 1)

    rc = pw.main(["PeerB", "--workspace", str(ws), "--json"])
    check("main() --json also exits 1 on the same input", rc == 1)

# --- 9. Malformed / missing timestamps -------------------------------------
# Not defensive padding: this file is written by ANOTHER host and reaches us
# through a git sync, so a truncated or half-written value is a real arrival,
# not a hypothetical. Every one of these must land on NOT_ARMED — the verdict
# that says "I cannot judge" — and never on a healthy one.
check("_iso('') is None", pw._iso("") is None)
check("_iso(None-ish empty) is None", pw._iso(None or "") is None)
check("_iso on a non-timestamp is None, not an exception",
      pw._iso("not-a-timestamp") is None)
check("_iso on a truncated ISO string is None",
      pw._iso("2026-08-02T10:") is None)

v = pw.evaluate({"state": "back", "valid_for_minutes": 45},
                committed=T("2026-08-02T10:40:21Z"), now=T("2026-08-02T10:41:00Z"))
check("a signal file with NO heartbeat_at is NOT_ARMED, never healthy",
      v["verdict"] == "NOT_ARMED" and v["exit"] == 1, str(v))

v = pw.evaluate({"state": "back", "heartbeat_at": "garbage", "valid_for_minutes": 45},
                committed=T("2026-08-02T10:40:21Z"), now=T("2026-08-02T10:41:00Z"))
check("an UNPARSEABLE heartbeat_at is NOT_ARMED, never healthy",
      v["verdict"] == "NOT_ARMED" and v["exit"] == 1, str(v))

# --- 10. A stale VIEW must not read as healthy forever ----------------------
# Review canary on #2515: a peer that dies immediately after publishing a healthy
# snapshot can never publish again, so an age-blind reader returns ALIVE_AS_OF /
# exit 0 indefinitely. Measured before the fix: beat_lag 1.0 min, snapshot_age
# 306719 min (7 months), verdict ALIVE_AS_OF. The mirror of the bug this module
# fixes — I over-corrected against wall-clock and made the reader permanently blind.
STALE = doc(heartbeat_at="2026-01-01T00:00:00Z", valid_until="2026-01-01T00:45:00Z")
v = pw.evaluate(STALE, committed=T("2026-01-01T00:01:00Z"), now=T("2026-08-02T00:00:00Z"))
check("a 7-month-old healthy snapshot is VIEW_STALE, not ALIVE_AS_OF",
      v["verdict"] == "VIEW_STALE", str(v))
check("...and it is UNKNOWN (exit 1), not an escalation (exit 2)",
      v["exit"] == 1, str(v))
check("...and the beat lag is still reported as healthy — the PEER was fine",
      v.get("beat_lag_min") == 1.0, str(v))
check("...and the detail says 'cannot tell', not that the peer failed",
      "cannot tell" in v["detail"], v["detail"])

# The bound is the PEER's, not ours: derive it from valid_for_minutes when the
# explicit field is absent, so an older signal file still gets a freshness check.
v = pw.evaluate({"state": "back", "heartbeat_at": "2026-01-01T00:00:00Z",
                 "valid_for_minutes": 45},
                committed=T("2026-01-01T00:01:00Z"), now=T("2026-08-02T00:00:00Z"))
check("a signal file with no valid_until falls back to beat + valid_for_minutes",
      v["verdict"] == "VIEW_STALE", str(v))

# And the boundary must not fire early — inside the peer's window stays healthy.
v = pw.evaluate(doc(heartbeat_at="2026-08-02T10:00:00Z",
                    valid_until="2026-08-02T10:45:00Z"),
                committed=T("2026-08-02T10:05:00Z"), now=T("2026-08-02T10:40:00Z"))
check("inside the peer's own valid_until the view is still ALIVE_AS_OF",
      v["verdict"] == "ALIVE_AS_OF" and v["exit"] == 0, str(v))

# A genuinely stopped beat still outranks staleness — it is the stronger claim.
v = pw.evaluate(doc(heartbeat_at="2026-01-01T00:00:00Z", valid_until="2026-01-01T00:45:00Z"),
                committed=T("2026-01-01T09:00:00Z"), now=T("2026-08-02T00:00:00Z"))
check("BEAT_STOPPED still wins over VIEW_STALE when the peer had gone quiet",
      v["verdict"] == "BEAT_STOPPED" and v["exit"] == 2, str(v))

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — a stale view reads as stale, a stopped peer reads as stopped")
