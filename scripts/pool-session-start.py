#!/usr/bin/env python3
"""Decide how a pool core should start, and record the decision durably.

Composition root for continuity: binds `pool_profiles` (the record) to
`pool_resume` (the policy) and prints the launch arguments as JSON for the
wrapper to exec. The wrapper stays a dumb adapter — it never decides.

Two properties the wrapper cannot provide for itself:

The session id is chosen HERE and written to the profile BEFORE exec, on any
runtime that can pre-assign one. A core that dies between starting and
recording its id would otherwise strand a live session nothing points at.

The seat epoch is compare-and-swapped at this moment, not merely read. The
epoch in the record is a fact; refusing to emit launch arguments when it no
longer matches is what actually stops two cores driving one session.

Usage:
  pool-session-start.py --workspace W --core core-1 --runtime claude
  pool-session-start.py ... --outcome ok|fail --session-id S   (report back)
"""
from __future__ import annotations

# flake8: noqa: E402 — imports follow the sys.path bootstrap

import argparse
import json
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))

from pool_profiles import (ProfileStore, ProfileStoreCorrupt, SeatFenced,
                           UnknownProfile)
from pool_resume import BACKOFF, NEW, PROBE, RESUME, decide, runtime_capability

LEAD_LABEL = "pool-lead"


def _store(workspace: Path) -> ProfileStore:
    return ProfileStore(Path(workspace) / "state" / "pool" / "profiles.json",
                        lead_label=LEAD_LABEL)


def seated_profile(store: ProfileStore, core: str):
    """The profile this core currently holds, with its epoch. None when the
    lead has not seated it — a core with no profile is not an error yet."""
    try:
        data = store.load()
    except ProfileStoreCorrupt:
        return None
    for pid, prof in sorted(data["profiles"].items()):
        if prof["seat"]["core_id"] == core:
            return pid, prof
    return None


def launch_plan(store: ProfileStore, core: str, runtime: str,
                probe_ok=None, new_id_fn=lambda: str(uuid.uuid4())) -> dict:
    """Emit the arguments to exec with, having durably recorded the intent."""
    seated = seated_profile(store, core)
    if seated is None:
        return {"action": NEW, "args": [], "profile_id": None,
                "note": "no profile seated on this core; starting unmanaged"}
    pid, prof = seated
    epoch = prof["seat"]["epoch"]
    head = store.head(pid)
    plan = decide(head, prof.get("attempts", ()), probe_ok)

    if plan["action"] == BACKOFF:
        return {"action": BACKOFF, "args": [], "profile_id": pid,
                "note": plan["note"]}
    if plan["action"] == PROBE:
        # Same pane, same env, same runtime — the ONLY difference is that no
        # session is resumed, so a failure here is about the environment.
        return {"action": PROBE, "args": [], "profile_id": pid,
                "probe": True, "note": plan["note"]}
    if plan["action"] == RESUME:
        return {"action": RESUME, "args": ["--resume", plan["session_id"]],
                "profile_id": pid, "seat_epoch": epoch,
                "session_id": plan["session_id"], "note": plan["note"]}

    cap = runtime_capability(runtime)
    gid = store.begin_generation(pid, core, epoch, runtime,
                                 plan.get("reason") or "initial")
    if not cap["preassign"]:
        return {"action": NEW, "args": [], "profile_id": pid,
                "seat_epoch": epoch, "generation_id": gid,
                "note": plan["note"] + f"; {runtime} cannot pre-assign an id, "
                                       f"so it is read back after start"}
    sid = new_id_fn()
    store.promote_generation(pid, core, epoch, gid, sid)
    return {"action": NEW, "args": ["--session-id", sid], "profile_id": pid,
            "seat_epoch": epoch, "generation_id": gid, "session_id": sid,
            "note": plan["note"]}


def report(store: ProfileStore, core: str, profile_id: str, seat_epoch: int,
           session_id, ok: bool) -> dict:
    store.record_attempt(profile_id, core, seat_epoch, session_id, ok)
    return {"recorded": bool(ok), "session_id": session_id}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--core", required=True)
    ap.add_argument("--runtime", default="claude")
    ap.add_argument("--probe-ok", choices=("0", "1"), default=None)
    ap.add_argument("--outcome", choices=("ok", "fail"), default=None)
    ap.add_argument("--profile-id")
    ap.add_argument("--seat-epoch", type=int)
    ap.add_argument("--session-id")
    a = ap.parse_args()
    store = _store(Path(a.workspace))
    try:
        if a.outcome:
            if not (a.profile_id and a.seat_epoch is not None):
                print(json.dumps({"error": "outcome needs --profile-id and "
                                           "--seat-epoch"}))
                return 2
            out = report(store, a.core, a.profile_id, a.seat_epoch,
                         a.session_id, a.outcome == "ok")
        else:
            probe = None if a.probe_ok is None else a.probe_ok == "1"
            out = launch_plan(store, a.core, a.runtime, probe)
    except SeatFenced as e:
        # Refusing to launch is the whole point: the lead moved this profile.
        print(json.dumps({"action": "refused", "reason": str(e)}))
        return 3
    except (UnknownProfile, ProfileStoreCorrupt) as e:
        print(json.dumps({"action": "refused", "reason": str(e)}))
        return 4
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
