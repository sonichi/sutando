#!/usr/bin/env python3
"""Design B prototype: the payload's LOCATION is the state; claiming moves the source.

    ready/<id>                       available
    inflight/<id>.<worker>.<pid>.<birth>   claimed by exactly one worker
    archive/<id>                     delivered
    undelivered/<id>                 parked

Every legal transition is one atomic rename. Two workers racing to claim compete
for the same SOURCE: the winner moves it, the loser gets ENOENT. There is no
destination-side CAS, no claim record, no tomb, no lock.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mainsrc"))
import outbox as ob  # reuse the ALIVE/DEAD/UNKNOWN process oracle  # noqa: E402

READY, INFLIGHT, ARCHIVE, PARKED = "ready", "inflight", "archive", "undelivered"


def _d(root, name):
    p = Path(root) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def publish(root, item_id, body=""):
    (_d(root, READY) / item_id).write_text(body, encoding="utf-8")


def claim(root, item_id, worker):
    """Claim by moving the source. Exactly one racer can move a given file."""
    ident = ob.process_identity(os.getpid())
    token = f"{item_id}.{worker}.{os.getpid()}.{ident.start_usec}"
    src = _d(root, READY) / item_id
    dst = _d(root, INFLIGHT) / token
    try:
        os.rename(str(src), str(dst))       # atomic; loser gets ENOENT
    except OSError:
        return None
    return token


def complete(root, token, terminal=ARCHIVE):
    item_id = token.split(".")[0]
    try:
        os.rename(str(_d(root, INFLIGHT) / token), str(_d(root, terminal) / item_id))
        return True
    except OSError:
        return False


def holder(root, item_id):
    for f in _d(root, INFLIGHT).iterdir():
        if f.name.split(".")[0] == item_id:
            return f.name.split(".")[1]
    return None


def recover(root):
    """Return items whose owner is provably DEAD to ready/. UNKNOWN is left alone."""
    moved = []
    for f in list(_d(root, INFLIGHT).iterdir()):
        parts = f.name.split(".")
        if len(parts) < 4:
            continue
        item_id, _worker, pid, birth = parts[0], parts[1], int(parts[2]), parts[3]
        ident = ob.process_identity(pid)
        if ident.state is not ob.OwnerState.DEAD:
            continue                        # ALIVE or UNKNOWN: never reclaim
        if str(ident.start_usec) != birth and ident.start_usec is not None:
            continue
        try:
            os.rename(str(f), str(_d(root, READY) / item_id))
            moved.append(item_id)
        except OSError:
            pass                            # someone else got there first
    return moved
