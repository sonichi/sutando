#!/usr/bin/env python3
"""Attribution for a terminal dialog Sutando opened itself, and the one rule that may dismiss it.

A component that types a command into the core's pane which can leave a picker on screen
(e.g. a `/model` send) writes a record here BEFORE the send and clears it once the CLI has
answered. The supervisor monitor (core-input-watch.py) reads it: a picker still on screen
`dismiss_after_s` after it appeared, first seen within the record's claim window, gets one
Escape. Without a record nothing is dismissed, so a picker a human opened is never touched.

The record names no skill; the opener chooses its own timings. Only `selection` gates are
eligible, the key is always Escape (cancel), and a prompt that mentions spend or usage
credits is never eligible whatever the record says.

Record: <state_dir>/self-opened-gate.<session>.json
  {"session", "opener", "kind": "selection", "opened_at", "claim_window_s", "dismiss_after_s"}

CLI (for shell openers):
  self_opened_gate.py record --state-dir D --session S --opener NAME --dismiss-after S [--claim-window S]
  self_opened_gate.py clear  --state-dir D --session S
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from typing import Optional

DISMISSABLE_KINDS = frozenset({"selection"})
DISMISS_KEY = "Escape"
DEFAULT_CLAIM_WINDOW_S = 60.0
#: The picker can paint a moment before the record's clock reads it opened.
_SKEW_S = 5.0
_SPEND = re.compile(r"\bcredits?\b|spend limit|extra usage|usage limit", re.I)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def record_path(state_dir: str, session: str) -> str:
    return os.path.join(state_dir, f"self-opened-gate.{_SAFE.sub('_', session) or 'core'}.json")


def record(state_dir: str, session: str, opener: str, dismiss_after_s: float,
           claim_window_s: float = DEFAULT_CLAIM_WINDOW_S, now: Optional[float] = None) -> dict:
    rec = {"session": session, "opener": opener, "kind": "selection",
           "opened_at": time.time() if now is None else now,
           "claim_window_s": float(claim_window_s), "dismiss_after_s": float(dismiss_after_s)}
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".self-opened-gate.")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, record_path(state_dir, session))
    return rec


def clear(state_dir: str, session: str) -> None:
    try:
        os.remove(record_path(state_dir, session))
    except FileNotFoundError:
        pass


def load(state_dir: str, session: str) -> Optional[dict]:
    """The record, or None when absent or malformed (malformed is never permission)."""
    try:
        with open(record_path(state_dir, session)) as f:
            rec = json.load(f)
        if not isinstance(rec, dict) or rec.get("session") != session:
            return None
        for k in ("opened_at", "claim_window_s", "dismiss_after_s"):
            rec[k] = float(rec[k])
        return rec
    except (OSError, ValueError, TypeError, KeyError):
        return None


def dismiss_key(rec: Optional[dict], *, session: str, state: str, kind: Optional[str],
                prompt: Optional[str], gate_first_seen: Optional[float],
                prompt_since: Optional[float], now: float) -> Optional[str]:
    """Escape when this blocked picker is the one `rec` attributes to Sutando and has sat
    unchanged for its dismiss delay; else None. Every unknown answers None."""
    if not rec or rec.get("session") != session or rec.get("kind") not in DISMISSABLE_KINDS:
        return None
    if state != "blocked-human" or kind not in DISMISSABLE_KINDS or not prompt or _SPEND.search(prompt):
        return None
    if gate_first_seen is None or prompt_since is None or rec["dismiss_after_s"] <= 0:
        return None
    opened = rec["opened_at"]
    if not (opened - _SKEW_S <= gate_first_seen <= opened + rec["claim_window_s"]):
        return None
    if now - prompt_since < rec["dismiss_after_s"]:
        return None
    return DISMISS_KEY


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    c = sub.add_parser("clear")
    for p in (r, c):
        p.add_argument("--state-dir", required=True)
        p.add_argument("--session", required=True)
    r.add_argument("--opener", required=True)
    r.add_argument("--dismiss-after", type=float, required=True)
    r.add_argument("--claim-window", type=float, default=DEFAULT_CLAIM_WINDOW_S)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "record":
            record(a.state_dir, a.session, a.opener, a.dismiss_after, a.claim_window)
        else:
            clear(a.state_dir, a.session)
    except OSError as exc:
        print(f"self_opened_gate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
