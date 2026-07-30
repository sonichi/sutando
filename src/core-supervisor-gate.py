#!/usr/bin/env python3
"""core-supervisor-gate.py — the RECOVER decision gate (sonichi#2401 prototype).

Decides whether a fully-dead core should be relaunched, with the compound
signal #2246 asks for. Relaunch is warranted ONLY when ALL THREE hold:

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

Usage (one tick; caller crons it ~60s):
  core-supervisor-gate.py tick --alive <ws>/state/cores/<host>.alive \
      --socket /tmp/sutando-tmux.sock --session sutando-core \
      --restart-sentinel <ws>/state/restart-in-progress.sentinel \
      --state-file <ws>/state/core-supervisor-gate.state \
      [--relaunch-cmd 'bash src/startup.sh'] [--dry-run]

Exit codes: 0 = healthy/holding (no action), 3 = gate tripped (dry-run
printed WOULD-RELAUNCH, or relaunch-cmd was executed).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


def evaluate(hb_stale: bool, session_gone: bool, operator_intent: bool) -> bool:
    """Pure compound gate: dead-heartbeat AND dead-session AND no operator intent."""
    return hb_stale and session_gone and not operator_intent


# ---- signal collectors (thin, each independently overridable in tests) ---- #
def heartbeat_stale(alive_path: str, stale_sec: float, now: float | None = None) -> bool:
    """Missing file counts as stale — a core that never wrote .alive is not alive."""
    try:
        mtime = os.stat(alive_path).st_mtime
    except OSError:
        return True
    return ((now if now is not None else time.time()) - mtime) > stale_sec


def session_gone(socket: str, session: str) -> bool:
    """True when tmux reports no such session (or no server at the socket)."""
    try:
        r = subprocess.run(["tmux", "-S", socket, "has-session", "-t", session],
                           capture_output=True, timeout=10)
        return r.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        # tmux binary missing/hung: can't prove the session exists → treat as
        # gone, but the heartbeat leg still gates (both must agree to trip).
        return True


def operator_intent(sentinel_path: str) -> bool:
    return bool(sentinel_path) and os.path.exists(sentinel_path)


# ---- sustained-streak persistence ---- #
def load_streak(state_file: str) -> int:
    try:
        with open(state_file) as f:
            d = json.load(f)
        return int(d.get("streak", 0)) if isinstance(d, dict) else 0
    except (OSError, ValueError):
        return 0


def save_streak(state_file: str, streak: int) -> None:
    d = os.path.dirname(state_file)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"streak": streak, "ts": time.time()}, f)
    os.replace(tmp, state_file)


def tick(args) -> int:
    """One evaluation. Returns the exit code (0 hold, 3 tripped)."""
    hb = heartbeat_stale(args.alive, args.stale_sec)
    gone = session_gone(args.socket, args.session)
    intent = operator_intent(args.restart_sentinel)
    tripped_now = evaluate(hb, gone, intent)

    streak = load_streak(args.state_file) + 1 if tripped_now else 0
    verdict = "TRIP" if tripped_now else "healthy"
    print(f"gate: hb_stale={hb} session_gone={gone} operator_intent={intent} "
          f"→ {verdict} (streak {streak}/{args.sustain})")

    if streak < args.sustain:
        save_streak(args.state_file, streak)
        return 0

    # Sustained trip → act once, then reset so the next death re-arms cleanly.
    save_streak(args.state_file, 0)
    if args.dry_run or not args.relaunch_cmd:
        print(f"WOULD-RELAUNCH: {args.relaunch_cmd or '<no relaunch-cmd configured>'}")
        return 3
    print(f"RELAUNCH: {args.relaunch_cmd}")
    subprocess.Popen(["bash", "-c", args.relaunch_cmd],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
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
    t.add_argument("--stale-sec", type=float, default=90.0)
    t.add_argument("--sustain", type=int, default=2,
                   help="consecutive tripped ticks required before acting")
    t.add_argument("--relaunch-cmd", default="", help="command to run on sustained trip")
    t.add_argument("--dry-run", action="store_true",
                   help="print WOULD-RELAUNCH instead of executing")
    args = ap.parse_args(argv)
    return tick(args)


if __name__ == "__main__":
    sys.exit(main())
