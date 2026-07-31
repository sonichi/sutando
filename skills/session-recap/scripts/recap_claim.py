#!/usr/bin/env python3
"""Atomic in-flight claim for the boot session recap.

The boot recap runs as a BACKGROUND subagent, so the completion stamp
(state/last-recap-session.txt) does not exist during the worker's window
(measured ~47s, budget 1-2 min). A mid-session /schedule-crons re-run inside
that window sees no stamp and would spawn a SECOND worker for the same
transcript — two concurrent summarizers racing last-session-recap.md and
double-posting the private-room brief. This script makes the reservation
atomic and is the required gate BEFORE spawning the worker:

  claim <session-uuid>              exit 0 -> caller may spawn the worker;
                                    prints "claimed token=<nonce>" — the
                                    launcher MUST pass that token to the
                                    worker for release.
                                    exit 1 -> skip (already recapped, or a
                                              live claim exists)
  release <session-uuid> --token T [--stamp]
                                    worker calls on completion; --stamp
                                    records the session as recapped. Without
                                    --stamp (failure path) the claim is
                                    dropped so a later run can retry. Both
                                    paths are OWNERSHIP-CHECKED: they only
                                    act if the on-disk claim still carries
                                    token T. A worker whose claim was
                                    stale-reclaimed gets exit 1 and must NOT
                                    touch the stamp — the reclaiming worker
                                    owns the session now.

Atomicity: the payload is written to a private temp file first, then
os.link()ed to the claim path (state/recap-inflight.json) — link is atomic
and fails if the target exists, so the kernel picks exactly one winner among
concurrent claimers AND a claim is never visible with a partial/empty
payload. (The first cut used O_CREAT|O_EXCL then wrote the payload into the
opened fd; a concurrent claimer could read the not-yet-written file, judge
it corrupt→stale, unlink it, and win too — 5-of-8 winners on the 2-core CI
runner.) A claim older than --stale-minutes (default 15) is a dead worker:
claimers unlink it and re-race, again with exactly one winner, so a crashed
worker can never wedge boot recaps.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_PARENT = Path(__file__).resolve().parent
REPO = SCRIPT_PARENT.parents[2]

CLAIM_NAME = "recap-inflight.json"
STAMP_NAME = "last-recap-session.txt"


def state_dir(override: str | None) -> Path:
    if override:
        return Path(override)
    ws = subprocess.run(
        ["bash", str(REPO / "scripts" / "sutando-config.sh"), "workspace"],
        capture_output=True, text=True, check=True).stdout.strip()
    return Path(ws) / "state"


def claim(sdir: Path, session: str, stale_s: float) -> int:
    stamp = sdir / STAMP_NAME
    try:
        if stamp.read_text().strip() == session:
            print("skip: already-recapped")
            return 1
    except OSError:
        pass  # no stamp yet — normal on a fresh boot
    path = sdir / CLAIM_NAME
    token = secrets.token_hex(8)
    payload = json.dumps({"session": session, "ts": time.time(),
                          "pid": os.getpid(), "token": token})
    # Full payload lands in a private temp file BEFORE the claim becomes
    # visible: os.link is atomic-or-FileExistsError, so no claimer can ever
    # observe a partial/empty claim (the corrupt→stale misread that produced
    # multiple winners with the create-then-write approach).
    fd, tmp = tempfile.mkstemp(dir=sdir, prefix=".recap-claim-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        # At most two rounds: a fresh claim skips inside round 1; a stale
        # claim is unlinked and re-raced once — the re-race loser then sees
        # the winner's fresh claim and skips.
        for _ in range(2):
            try:
                os.link(tmp, path)
            except FileExistsError:
                try:
                    cur = json.loads(path.read_text())
                except (OSError, ValueError):
                    cur = {}
                age = time.time() - float(cur.get("ts") or 0)
                if age < stale_s:
                    print(f"skip: in-flight ({cur.get('session', '?')}, "
                          f"{age:.0f}s old)")
                    return 1
                path.unlink(missing_ok=True)  # dead worker — re-race
                continue
            print(f"claimed token={token}")
            return 0
        print("skip: lost re-race")
        return 1
    finally:
        os.unlink(tmp)


def release(sdir: Path, session: str, stamp: bool, token: str) -> int:
    # Ownership gate: act only if the on-disk claim is still OURS. A worker
    # whose claim was stale-reclaimed must not unlink the reclaimer's live
    # reservation, and a late `release --stamp` must not stamp over the
    # reclaimer's in-progress run (the stale-A/reclaimed-B lifecycle race).
    path = sdir / CLAIM_NAME
    try:
        cur = json.loads(path.read_text())
    except (OSError, ValueError):
        print("skip: no live claim to release (reclaimed or already "
              "released) — not stamping")
        return 1
    if cur.get("token") != token or cur.get("session") != session:
        print(f"skip: live claim is not ours "
              f"(live session={cur.get('session', '?')}) — not stamping")
        return 1
    if stamp:
        tmp = sdir / (STAMP_NAME + ".tmp")
        tmp.write_text(session + "\n")
        os.replace(tmp, sdir / STAMP_NAME)
    path.unlink(missing_ok=True)
    print("released" + (" + stamped" if stamp else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["claim", "release"])
    ap.add_argument("session", help="session transcript uuid being recapped")
    ap.add_argument("--stamp", action="store_true",
                    help="release only: record the session as recapped")
    ap.add_argument("--token", default=None,
                    help="release only (required there): the ownership "
                         "token printed by the winning claim")
    ap.add_argument("--stale-minutes", type=float, default=15.0,
                    help="claims older than this are dead workers (default 15)")
    ap.add_argument("--state-dir", default=None,
                    help="state dir override (tests / worktree runs); "
                         "default <workspace>/state")
    args = ap.parse_args()
    sdir = state_dir(args.state_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    if args.cmd == "claim":
        return claim(sdir, args.session, args.stale_minutes * 60)
    if not args.token:
        ap.error("release requires --token (printed by the winning claim)")
    return release(sdir, args.session, args.stamp, args.token)


if __name__ == "__main__":
    sys.exit(main())
