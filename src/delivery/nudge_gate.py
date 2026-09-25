"""Whether a supervisor with no session-role watcher should nudge, alert, or arm.

The supervisor stands in for a session's own in-session Monitor task watcher.
When that watcher is missing, the old behaviour was to arm the external notifier
after a grace period, always. This module adds one decision in front of that arm:
when the session is idle with a clean composer AND is genuinely alive, prefer to
nudge it into re-arming its own watcher (single in-session owner) instead of
running the external standby.

The decision is a pure function of two signals the supervisor already has a way
to read:

- ``pane_state``: ``pane_gate.classify_pane``'s verdict. ``idle-ready`` means the
  agent is idle AND the composer is empty; anything else (``busy``, ``pending``
  text in the composer, ``abnormal``, ``unknown``) is not.
- ``health``: freshness of the session's liveness beat (``pool_beat.classify``):
  ``live`` under the stale window, else ``stale``/``absent``/``unknown``. This is
  stronger than "the tmux pane exists": a pane keeps showing an idle frame long
  after the agent behind it has hung or died.

Outcomes:

- ``nudge`` — idle-ready AND live. Inject one prompt that checks pending tasks and
  re-arms the Monitor, then let the grace run until the next idle; only if no
  session-role watcher appears does the supervisor fall back to arming.
- ``alert`` — idle-ready but NOT live (stale/absent beat). The frame looks idle
  over a dead or hung agent: a nudge is not read, and neither is an injected
  standby, so injection cannot fix it. Surface it for a restart (the owner's
  lane) instead of pretending the standby covers a corpse.
- ``arm`` — everything else, unchanged from before: a dirty or busy pane, or any
  UNKNOWN signal. Unknowns fail toward keeping the existing coverage, never
  toward withholding it, the same principle the supervisor already follows for an
  unreadable role verdict.
"""

from __future__ import annotations

# pane_gate verdict states (mirrored here so this module needs no import to be
# reasoned about; pane_gate remains the source of truth for producing them).
IDLE_READY = "idle-ready"
# beat freshness states, as pool_beat.classify emits them.
LIVE = "live"
STALE = "stale"
ABSENT = "absent"

NUDGE = "nudge"
ALERT = "alert"
ARM = "arm"


def decide(pane_state: str, health: str) -> str:
    """One of ``nudge`` / ``alert`` / ``arm`` from the pane and health signals.

    Only a definitive idle-ready pane leaves the arm path. On it, a live beat
    earns a nudge and a stale/absent beat earns an alert; an unknown beat, like
    every other uncertainty, arms. A pane that is anything but idle-ready arms,
    which is the supervisor's current behaviour untouched.
    """
    if pane_state == IDLE_READY:
        if health == LIVE:
            return NUDGE
        if health in (STALE, ABSENT):
            return ALERT
        # health unknown: do not withhold coverage on uncertainty.
        return ARM
    return ARM


def _cli(argv=None) -> int:
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Decide nudge / alert / arm for a missing session watcher.")
    ap.add_argument("--pane-state", required=True, help="pane_gate verdict state (idle-ready, busy, pending, abnormal, unknown)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--health", help="beat state directly (live, stale, absent, unknown)")
    group.add_argument("--beat-path", help="path to the liveness beat file; its freshness is classified")
    ap.add_argument("--stale-s", type=float, default=90.0, help="seconds after which a beat is stale (default 90)")
    args = ap.parse_args(argv)

    health = args.health
    if health is None:
        # Resolve freshness from the beat file's mtime, three-valued like
        # pool_beat: absent (no file) is never the same as stale (an old file).
        import os

        try:
            mtime = os.stat(args.beat_path).st_mtime
        except FileNotFoundError:
            health = ABSENT
        except OSError:
            health = "unknown"
        else:
            age = time.time() - mtime
            # A future-dated beat is treated as stale, matching pool_beat.
            health = LIVE if 0 <= age <= args.stale_s else STALE

    print(decide(args.pane_state, health))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
