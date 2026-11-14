#!/usr/bin/env python3
"""Runtime observations: one leased record per seat, written by whatever observes that seat's CLI.

    python3 src/runtime_observation.py write [--workspace DIR]   # one JSON record on stdin

A record says what an observer saw (motion, condition, reason) and carries its own heartbeat;
a reader trusts it only inside the lease. Nothing here names an observer.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_lock import locked_file
from workspace_default import resolve_workspace, status_path, write_status

SCHEMA = 1
LEASE_S = 45.0
FUTURE_TOLERANCE_S = 5.0
MAX_BYTES = 4096
DIR = "runtime-observations"

SEAT_RE = re.compile(r"core|[0-9a-f]{32}")
PHASES = ("requesting", "tool", "idle", "failed", "waiting", "compacting", "unknown")
MOTIONS = ("moving", "idle", "unknown")
CONDITIONS = ("healthy", "abnormal", "unknown")
REASONS = (None, "needs-login", "quota-limit", "out-of-credits", "api-error", "permission", "awaiting-input")

# field -> (max length, nullable)
_STRINGS = {"observer": (40, False), "observer_version": (40, False), "observer_id": (64, False),
            "session": (80, False), "claude_session_id": (80, True)}
_EPOCHS = {"observer_started_at": False, "changed_at": False, "condition_since": True,
           "last_success_at": True, "heartbeat_at": False}
_ENUMS = {"phase": PHASES, "motion": MOTIONS, "condition": CONDITIONS, "reason": REASONS}


def _epoch(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def validate(record) -> dict:
    """The record reduced to schema 1 fields; ValueError on any bad value."""
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    if record.get("schema") != SCHEMA or isinstance(record.get("schema"), bool):
        raise ValueError("unsupported schema")
    seat = record.get("seat")
    if not isinstance(seat, str) or not SEAT_RE.fullmatch(seat):
        raise ValueError("seat must be 'core' or a 32-hex worker id")
    out = {"schema": SCHEMA, "seat": seat}
    for key, (limit, nullable) in _STRINGS.items():
        value = record.get(key)
        if value is None and nullable:
            out[key] = None
        elif isinstance(value, str) and 0 < len(value) <= limit:
            out[key] = value
        else:
            raise ValueError(f"bad {key}")
    for key, nullable in _EPOCHS.items():
        value = record.get(key)
        if value is None and nullable:
            out[key] = None
        elif _epoch(value):
            out[key] = float(value)
        else:
            raise ValueError(f"bad {key}")
    seq = record.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("bad seq")
    out["seq"] = seq
    for key, allowed in _ENUMS.items():
        if record.get(key) not in allowed:
            raise ValueError(f"bad {key}")
        out[key] = record[key]
    if (out["condition"] == "abnormal") != (out["reason"] is not None):
        raise ValueError("reason is required exactly when abnormal")
    incarnation = record.get("core_incarnation")
    if incarnation is not None:
        if (not isinstance(incarnation, str) or not 1 <= len(incarnation) <= 160
                or "\n" in incarnation or "\r" in incarnation):
            raise ValueError("bad core_incarnation")
        out["core_incarnation"] = incarnation
    completed = record.get("completed_task")
    if completed is not None:
        if not isinstance(completed, dict):
            raise ValueError("bad completed_task")
        digest = completed.get("prompt_sha256")
        incarnation = completed.get("core_incarnation")
        accepted, finished = completed.get("accepted_at"), completed.get("completed_at")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(incarnation, str) or not 1 <= len(incarnation) <= 160
                or "\n" in incarnation or "\r" in incarnation
                or not _epoch(accepted) or not _epoch(finished)
                or accepted > finished or finished > out["heartbeat_at"]
                or out["phase"] != "idle" or out["motion"] != "idle"
                or out["condition"] != "healthy"):
            raise ValueError("bad completed_task")
        out["completed_task"] = {"prompt_sha256": digest, "core_incarnation": incarnation,
                                 "accepted_at": float(accepted), "completed_at": float(finished)}
    last_completed = record.get("last_completed_at")
    if last_completed is not None:
        if not _epoch(last_completed) or last_completed > out["heartbeat_at"]:
            raise ValueError("bad last_completed_at")
        out["last_completed_at"] = float(last_completed)
    failed = record.get("failed_task")
    if failed is not None:
        if not isinstance(failed, dict):
            raise ValueError("bad failed_task")
        digest, turn = failed.get("prompt_sha256"), failed.get("turn_id")
        incarnation = failed.get("core_incarnation")
        accepted, finished = failed.get("accepted_at"), failed.get("completed_at")
        kind = failed.get("error_kind")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(turn, str) or not 1 <= len(turn) <= 128
                or any(ord(c) < 32 for c in turn)
                or incarnation != out.get("core_incarnation") or not incarnation
                or not _epoch(accepted) or not _epoch(finished) or accepted > finished
                or finished > out["heartbeat_at"] or out["phase"] != "failed"
                or out["motion"] != "idle" or out["condition"] != "abnormal"
                or kind not in ("server-overloaded", "api-error", "quota-limit", "needs-login")
                or out["reason"] != ("api-error" if kind == "server-overloaded" else kind)):
            raise ValueError("bad failed_task")
        out["failed_task"] = {"turn_id": turn, "prompt_sha256": digest,
                              "core_incarnation": incarnation, "accepted_at": float(accepted),
                              "completed_at": float(finished), "error_kind": kind}
    return out


def record_path(ws: Path, seat: str) -> Path:
    return status_path(f"{DIR}/{seat}.json", ws)


def _read(path: Path):
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _superseded(old, new) -> bool:
    """A record from the same observer that is behind the stored one."""
    if not old or old["observer_id"] != new["observer_id"]:
        return False
    return new["seq"] < old["seq"] or (new["seq"] == old["seq"] and new["heartbeat_at"] <= old["heartbeat_at"])


def write(record, ws=None) -> bool:
    """Store a record; False when it is older than the same observer's stored one."""
    rec = validate(record)
    ws = Path(ws) if ws is not None else Path(resolve_workspace(migrate=False))
    path = record_path(ws, rec["seat"])
    with locked_file(path.with_name(f".{rec['seat']}.lock")):
        if _superseded(_read(path), rec):
            return False
        write_status(f"{DIR}/{rec['seat']}.json", rec, ws)
    return True


def load(ws, seat: str, now: float):
    """(record | None, why): why is None, 'missing', 'invalid', 'expired' or 'future'."""
    path = record_path(Path(ws), seat)
    if not path.exists():
        return None, "missing"
    rec = _read(path)
    if rec is None or rec["seat"] != seat:
        return None, "invalid"
    if rec["heartbeat_at"] - now > FUTURE_TOLERANCE_S:
        return None, "future"
    if now - rec["heartbeat_at"] > LEASE_S:
        return None, "expired"
    return rec, None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["write"])
    ap.add_argument("--workspace", default=None)
    args = ap.parse_args(argv)
    raw = sys.stdin.read(MAX_BYTES + 1)
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("record too large")
        accepted = write(json.loads(raw), args.workspace)
    except (ValueError, OSError) as exc:
        print(f"runtime_observation: rejected: {exc}", file=sys.stderr)
        return 2
    if not accepted:
        print("runtime_observation: stale record ignored", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
