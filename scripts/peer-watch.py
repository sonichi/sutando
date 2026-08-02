#!/usr/bin/env python3
"""Read a peer host's restart-watch signal WITHOUT confusing a stale view for a dead peer.

`<workspace>/hosts/<peer>/restart-watch.json` is how one core learns whether
another came back from a restart. The peer bumps `heartbeat_at`/`valid_until`
each pass; the reader sees the file only after it has travelled through the
vault, which is a git remote polled on an interval.

**The bug this exists to prevent (measured 2026-08-02, Pro reading Mini):**
comparing `valid_until` against WALL-CLOCK conflates two independent things —

    did the PEER stop beating?          <- a real problem
    is MY COPY of the file old?         <- a transport artifact

At 11:11Z Pro read `heartbeat_at 10:29:57Z`, called the beat "stopped", and
reported the peer possibly down. The peer had beaten at 10:29 · 10:55 · 11:11
and was entirely healthy; the file Pro held had been committed at 10:40:21Z and
simply had not been re-pulled. Arithmetic of the worst case, with real numbers:

    peer beat interval  ~26 min   (a session cron; it STRETCHES under load)
  + peer push interval   15 min
  + reader pull lag      ~3 min
  = ~44 min of structural staleness   vs a 45-min validity window

~1 minute of headroom, so a healthy peer trips it routinely. Widening the window
does not fix it — it delays the same false alarm and blinds the reader for
longer. The fix is to stop asking the question the transport cannot answer.

**So: compare `heartbeat_at` against the COMMIT TIME of the file, not `now`.**
"Was this host beating as of the snapshot I hold?" is answerable and transport
independent. "Is it beating right now?" is not answerable over a delayed vault,
and every attempt to answer it produces false UNKNOWNs on healthy hosts.

Commit time, not mtime: mtime moves for reasons unrelated to the peer — a sync,
a checkout, a merge — so it measures this host's git activity, not the peer's.

Every threshold is read from the file itself (`valid_for_minutes`,
`expected_back_by`), so the peer declares its own tolerances and this reader
invents none. Snapshot age is REPORTED but never escalated on: without knowing
the sync interval, any staleness cutoff here would be self-invented, which is
the class of guess that produced the original false alarm.

Verdicts:
  COMEBACK_FAILED  state=down, past expected_back_by, no came_back_at (exit 2)
  BEAT_STOPPED     peer stopped beating as of its own snapshot        (exit 2)
  ALIVE_AS_OF      beating normally as of the snapshot                (exit 0)
  NOT_ARMED        no signal file / never armed                       (exit 1)

Usage:
  python3 scripts/peer-watch.py <peer-host-label> [--json] [--workspace PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _iso(s: str) -> "dt.datetime | None":
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _workspace() -> Path:
    out = subprocess.run(["bash", str(REPO / "scripts/sutando-config.sh"), "workspace"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit(f"peer-watch: could not resolve workspace: {out.stderr.strip()[:200]}")
    return Path(out.stdout.strip())


def commit_time(workspace: Path, rel: str) -> "dt.datetime | None":
    """When the vault last committed this file. None if untracked/not a repo.

    Deliberately NOT mtime: a sync or checkout rewrites mtime on files the peer
    never touched, so mtime measures OUR git activity and would make a stale
    snapshot look fresh — the exact inversion this module exists to prevent.
    """
    r = subprocess.run(["git", "-C", str(workspace), "log", "-1", "--format=%cI", "--", rel],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return _iso(r.stdout.strip())


def evaluate(doc: dict, committed: "dt.datetime | None", now: dt.datetime) -> dict:
    """Pure verdict function — no I/O, so the table below can be exercised directly."""
    state = doc.get("state")
    beat = _iso(doc.get("heartbeat_at", ""))
    window_min = doc.get("valid_for_minutes")

    # 1. A declared restart that never came back. Per the protocol this IGNORES
    #    freshness: a stopped heartbeat during a declared restart is the expected
    #    condition, not a reason to downgrade to UNKNOWN.
    if state == "down":
        eta = _iso(doc.get("expected_back_by", ""))
        if eta and now > eta and not doc.get("came_back_at"):
            return {"verdict": "COMEBACK_FAILED", "exit": 2,
                    "detail": f"declared down at {doc.get('went_down_at')}, expected back by "
                              f"{doc.get('expected_back_by')}, no came_back_at "
                              f"({(now - eta).total_seconds() / 60:.0f} min overdue)"}

    if beat is None:
        return {"verdict": "NOT_ARMED", "exit": 1, "detail": "no readable heartbeat_at"}

    snapshot_age_min = (now - committed).total_seconds() / 60 if committed else None

    # 2. The load-bearing comparison: the beat against the SNAPSHOT that carried
    #    it, never against wall-clock.
    if committed is None:
        return {"verdict": "NOT_ARMED", "exit": 1,
                "detail": "file is not tracked in the vault — no commit time to judge against"}

    beat_lag_min = (committed - beat).total_seconds() / 60
    if window_min is not None and beat_lag_min > window_min:
        return {"verdict": "BEAT_STOPPED", "exit": 2,
                "beat_lag_min": round(beat_lag_min, 1),
                "snapshot_age_min": round(snapshot_age_min, 1),
                "detail": f"peer had not beaten for {beat_lag_min:.0f} min when it published this "
                          f"snapshot (its own window is {window_min} min) — the peer stopped, "
                          f"this is not transport lag"}

    return {"verdict": "ALIVE_AS_OF", "exit": 0,
            "beat_lag_min": round(beat_lag_min, 1),
            "snapshot_age_min": round(snapshot_age_min, 1),
            "detail": f"beating normally as of its snapshot (lag {beat_lag_min:.0f} min, window "
                      f"{window_min} min). My copy is {snapshot_age_min:.0f} min old — reported, "
                      f"not escalated: a staleness cutoff here would be self-invented."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("peer", help="peer host label, e.g. Chis-Mac-mini")
    ap.add_argument("--json", action="store_true", help="emit the verdict as JSON")
    ap.add_argument("--workspace", help=(
        "override the resolved workspace. Needed because this script resolves the "
        "workspace through the repo it LIVES in, so running it from a git worktree "
        "points it at <worktree>/workspace/ and it reports NOT_ARMED for a peer that "
        "is armed in the live workspace. Caught doing exactly that while testing."))
    args = ap.parse_args(argv)

    ws = Path(args.workspace) if args.workspace else _workspace()
    rel = f"hosts/{args.peer}/restart-watch.json"
    path = ws / rel
    if not path.is_file():
        out = {"verdict": "NOT_ARMED", "exit": 1, "detail": f"no signal file at {rel}"}
    else:
        doc = json.loads(path.read_text())
        out = evaluate(doc, commit_time(ws, rel), dt.datetime.now(dt.timezone.utc))

    out["peer"] = args.peer
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"{args.peer}: {out['verdict']}")
        print(f"  {out['detail']}")
    return out["exit"]


if __name__ == "__main__":
    sys.exit(main())
