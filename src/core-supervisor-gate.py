#!/usr/bin/env python3
"""core-supervisor-gate.py — the RECOVER decision gate (sonichi#2401 prototype).

Decides whether a fully-dead core should be REPORTED as dead, with the
compound signal #2246 asks for. A death verdict requires ALL THREE:

  1. heartbeat stale — `state/cores/<host>.alive` mtime older than --stale-sec
     (default 90, the documented staleness threshold every reader trusts)
  2. session gone — `tmux -S <socket> has-session -t <session>` fails: the
     core process is actually absent, not merely quiet
  3. no operator intent — the --restart-sentinel file is absent, so an
     intentional restart/migration in progress is never raced

sustained for --sustain consecutive ticks (default 2). Any signal false →
streak resets to 0. This is what the #1428 watchdog lacked: it keyed on a
single soft signal and destructively restarted working cores; here a
wedged-but-alive core (fresh heartbeat OR live session) can never trip the
gate, and an operator's planned restart is explicitly out of scope.

Scope: this module only DECIDES. The relaunch action belongs to the caller
(Sutando.app timer or the #2399 LaunchAgent) — both run in the GUI session,
so a relaunch under an unchanged, already-authenticated CLAUDE_CONFIG_DIR
comes up authenticated. A planned fire that CHANGES the config dir is the
#2402 pre-fire preflight's job; no supervisor can /login for the user.

NOTIFICATION-ONLY (owner redirect on #2401): a sustained trip exits 3 and
prints CORE-DEAD for the caller to surface as a notification. This module
never restarts anything — restart is human-triggered (app menu / chat
command, PR #2408).

Usage (one tick; caller crons it ~60s):
  core-supervisor-gate.py tick --alive <ws>/state/cores/<host>.alive \
      --socket /tmp/sutando-tmux.sock --session sutando-core \
      --restart-sentinel <ws>/state/restart-in-progress.sentinel \
      --state-file <ws>/state/core-supervisor-gate.state

Exit codes: 0 = healthy/holding (no action), 3 = sustained trip
(CORE-DEAD printed — the caller surfaces it as a notification).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def _positive_int(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("--sustain must be >= 1")
    return n


def _positive_float(v: str) -> float:
    n = float(v)
    if n <= 0:
        raise argparse.ArgumentTypeError("--stale-sec must be > 0")
    return n


def evaluate(hb_stale: bool, session_gone: bool | None, operator_intent: bool) -> bool:
    """Pure compound gate: dead-heartbeat AND CONFIRMED-dead-session AND no
    operator intent. session_gone=None (probe unknown) never trips."""
    return hb_stale and session_gone is True and not operator_intent


# ---- signal collectors (thin, each independently overridable in tests) ---- #
def heartbeat_stale(alive_path: str, stale_sec: float, now: float | None = None) -> bool:
    """Missing file counts as stale — a core that never wrote .alive is not alive."""
    try:
        mtime = os.stat(alive_path).st_mtime
    except OSError:
        return True
    return ((now if now is not None else time.time()) - mtime) > stale_sec


def session_gone(socket: str, session: str) -> bool | None:
    """True = session definitely absent, False = definitely present,
    None = probe failed (tmux missing/hung) — UNKNOWN, never confirmed death.
    The gate holds on None (fail-closed): a broken probe must not classify a
    possibly-running core as dead (john-the-dev review, #2404)."""
    try:
        r = subprocess.run(["tmux", "-S", socket, "has-session", "-t", session],
                           capture_output=True, timeout=10)
        return r.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return None


def operator_intent(sentinel_path: str) -> bool:
    return bool(sentinel_path) and os.path.exists(sentinel_path)


# ---- sustained-streak + reported-latch persistence ---- #
def load_state(state_file: str) -> tuple[int, bool]:
    """Returns (streak, reported). reported=True means the CURRENT outage has
    already been surfaced — it latches until a healthy/holding tick clears it,
    so one persistent outage can never notify more than once (qingyun +
    john-the-dev review, #2404)."""
    try:
        with open(state_file) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return 0, False
        return int(d.get("streak", 0)), bool(d.get("reported", False))
    except (OSError, ValueError):
        return 0, False


def save_state(state_file: str, streak: int, reported: bool) -> None:
    d = os.path.dirname(state_file)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"streak": streak, "reported": reported, "ts": time.time()}, f)
    os.replace(tmp, state_file)


def tick(args) -> int:
    """One evaluation. Returns the exit code (0 hold/already-reported, 3 =
    NEW sustained trip — reported exactly once per outage)."""
    hb = heartbeat_stale(args.alive, args.stale_sec)
    gone = session_gone(args.socket, args.session)
    intent = operator_intent(args.restart_sentinel)
    tripped_now = evaluate(hb, gone, intent)
    if gone is None:
        print("gate: session probe UNKNOWN (tmux error) — holding, not confirmed death")

    prev_streak, reported = load_state(args.state_file)
    streak = prev_streak + 1 if tripped_now else 0
    verdict = "TRIP" if tripped_now else "healthy"
    print(f"gate: hb_stale={hb} session_gone={gone} operator_intent={intent} "
          f"→ {verdict} (streak {streak}/{args.sustain}, reported={reported})")

    if not tripped_now:
        # Healthy or holding observation: clears the streak AND the reported
        # latch, so the NEXT sustained death is a new outage and reports again.
        save_state(args.state_file, 0, False)
        return 0

    if streak < args.sustain:
        save_state(args.state_file, streak, reported)
        return 0

    if reported:
        # Same outage, already surfaced — stay silent. Without this latch a
        # persistent outage would re-earn the threshold and notify every
        # --sustain ticks (the notification-storm case both reviewers repro'd).
        save_state(args.state_file, streak, True)
        print("gate: sustained death CONTINUES (already reported — not re-notifying)")
        return 0

    # NEW sustained trip → report exactly once; the latch holds until a
    # healthy/holding tick clears it. NOTIFICATION-ONLY by owner decision on
    # #2401: this gate never executes a relaunch — exit 3 is the signal the
    # notification layer (and a human-triggered restart, #2408) act on.
    save_state(args.state_file, streak, True)
    print("CORE-DEAD: sustained compound signal — notify the owner; "
          "restart is human-triggered (menu / 'restart core' chat command)")
    return 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick", help="run one gate evaluation")
    t.add_argument("--alive", required=True, help="path to state/cores/<host>.alive")
    t.add_argument("--socket", required=True, help="tmux socket path")
    t.add_argument("--session", default="sutando-core", help="tmux session name")
    t.add_argument("--restart-sentinel", default="", help="operator-intent sentinel path")
    t.add_argument("--state-file", required=True, help="streak persistence path")
    t.add_argument("--stale-sec", type=_positive_float, default=90.0,
                   help="heartbeat staleness threshold in seconds (> 0)")
    t.add_argument("--sustain", type=_positive_int, default=2,
                   help="consecutive tripped ticks required before reporting (>=1)")
    args = ap.parse_args(argv)
    return tick(args)


if __name__ == "__main__":
    sys.exit(main())
